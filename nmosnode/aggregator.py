# Copyright 2019 British Broadcasting Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from gevent import monkey
monkey.patch_all()
# The below imports allow for modules to be mocked in test_aggregator.py
import gevent # noqa E402
import gevent.queue  # noqa E402

import requests # noqa E402
import json # noqa E402
import time # noqa E402
import traceback # noqa E402
import webbrowser  # noqa E402
from six import itervalues # noqa E402
from six.moves.urllib.parse import urljoin # noqa E402
from socket import getfqdn  # noqa E402
from authlib.oauth2.rfc6750 import InvalidTokenError # noqa E402
from authlib.oauth2 import OAuth2Error # noqa E402

from mdnsbridge.mdnsbridgeclient import IppmDNSBridge # noqa E402
from nmoscommon.logger import Logger # noqa E402
from nmoscommon.mdns.mdnsExceptions import ServiceNotFoundException # noqa E402
from nmoscommon.nmoscommonconfig import config as _config # noqa E402

from .authclient import AuthRegistrar # noqa E402

APINAMESPACE = "x-nmos"

AGGREGATOR_APINAME = "registration"
AGGREGATOR_APIVERSION = _config.get('nodefacade').get('NODE_REGVERSION')
AGGREGATOR_APIROOT = '/' + APINAMESPACE + '/' + AGGREGATOR_APINAME + '/'

NODE_APINAME = "node"
NODE_APIROOT = '/' + APINAMESPACE + '/' + NODE_APINAME + '/'

LEGACY_REG_MDNSTYPE = "nmos-registration"
REGISTRATION_MDNSTYPE = "nmos-register"

OAUTH_MODE = _config.get("oauth_mode", False)
ALLOWED_GRANTS = ["authorization_code", "refresh_token", "client_credentials"]
ALLOWED_SCOPE = "is-04"


class NoAggregator(Exception):
    def __init__(self, mdns_updater=None):
        if mdns_updater is not None:
            mdns_updater.inc_P2P_enable_count()
        super(NoAggregator, self).__init__("No Registration API found")


class InvalidRequest(Exception):
    def __init__(self, status_code=400, mdns_updater=None):
        if mdns_updater is not None:
            mdns_updater.inc_P2P_enable_count()
        super(InvalidRequest, self).__init__("Invalid Request, code {}".format(status_code))
        self.status_code = status_code


class TooManyRetries(Exception):
    def __init__(self, mdns_updater=None):
        if mdns_updater is not None:
            mdns_updater.inc_P2P_enable_count()
        super(TooManyRetries, self).__init__("Too many retries.")


class Aggregator(object):
    """This class serves as a proxy for the distant aggregation service running elsewhere on the network.
    It will search out aggregators and locate them, falling back to other ones if the one it is connected to
    disappears, and resending data as needed."""
    def __init__(self, logger=None, mdns_updater=None, auth_registry=None):
        self.logger = Logger("aggregator_proxy", logger)
        self.mdnsbridge = IppmDNSBridge(logger=self.logger)
        self.aggregator = ""
        self.registration_order = ["device", "source", "flow", "sender", "receiver"]
        self._mdns_updater = mdns_updater
        # 'registered' is a local mirror of aggregated items. There are helper methods
        # for manipulating this below.
        self._registered = {
            'node': None,
            'registered': False,
            'auth_client_registered': False,
            'entities': {
                'resource': {
                }
            }
        }
        self.auth_registrar = None  # Class responsible for registering with Auth Server
        self.auth_registry = auth_registry  # Top level class that tracks locally registered OAuth clients
        self.auth_client = None  # Instance of Oauth client responsible for performing token requests

        self._running = True
        self._reg_queue = gevent.queue.Queue()
        self.heartbeat_thread = gevent.spawn(self._heartbeat)
        self.queue_thread = gevent.spawn(self._process_queue)

    def _heartbeat(self):
        """The heartbeat thread runs in the background every five seconds.
        If when it runs the Node is believed to be registered it will perform a heartbeat"""
        self.logger.writeDebug("Starting heartbeat thread")
        while self._running:
            heartbeat_wait = 5
            if not self._registered["registered"]:
                self._process_reregister()
            elif self._registered["node"]:
                # Do heartbeat
                try:
                    self.logger.writeDebug("Sending heartbeat for Node {}"
                                           .format(self._registered["node"]["data"]["id"]))
                    self._SEND("POST", "/health/nodes/" + self._registered["node"]["data"]["id"])
                except InvalidRequest as e:
                    if e.status_code == 404:
                        # Re-register
                        self.logger.writeWarning("404 error on heartbeat. Marking Node for re-registration")
                        self._registered["registered"] = False

                        if(self._mdns_updater is not None):
                            self._mdns_updater.inc_P2P_enable_count()
                    else:
                        # Client side error. Report this upwards via exception, but don't resend
                        self.logger.writeError("Unrecoverable error code {} received from Registration API on heartbeat"
                                               .format(e.status_code))
                        self._running = False
                except Exception as e:
                    # Re-register
                    self.logger.writeWarning(
                        "Unexpected error on heartbeat: {}. Marking Node for re-registration".format(e)
                    )
                    self._registered["registered"] = False
            else:
                self._registered["registered"] = False
                if(self._mdns_updater is not None):
                    self._mdns_updater.inc_P2P_enable_count()
            while heartbeat_wait > 0 and self._running:
                gevent.sleep(1)
                heartbeat_wait -= 1
        self.logger.writeDebug("Stopping heartbeat thread")

    def _process_queue(self):
        """Provided the Node is believed to be correctly registered, hand off a single request to the SEND method.
        On client error, clear the resource from the local mirror.
        On other error, mark Node as unregistered and trigger re-registration"""
        self.logger.writeDebug("Starting HTTP queue processing thread")
        # Checks queue not empty before quitting to make sure unregister node gets done
        while self._running or (self._registered["registered"] and not self._reg_queue.empty()):
            if not self._registered["registered"] or self._reg_queue.empty():
                gevent.sleep(1)
            else:
                try:
                    queue_item = self._reg_queue.get()
                    namespace = queue_item["namespace"]
                    res_type = queue_item["res_type"]
                    res_key = queue_item["key"]
                    if queue_item["method"] == "POST":
                        if res_type == "node":
                            data = self._registered["node"]
                            try:
                                self.logger.writeInfo("Attempting registration for Node {}"
                                                      .format(self._registered["node"]["data"]["id"]))
                                self._SEND("POST", "/{}".format(namespace), data)
                                self._SEND("POST", "/health/nodes/" + self._registered["node"]["data"]["id"])
                                self._registered["registered"] = True
                                if self._mdns_updater is not None:
                                    self._mdns_updater.P2P_disable()

                            except Exception:
                                self.logger.writeWarning("Error registering Node: %r" % (traceback.format_exc(),))

                        elif res_key in self._registered["entities"][namespace][res_type]:
                            data = self._registered["entities"][namespace][res_type][res_key]
                            try:
                                self._SEND("POST", "/{}".format(namespace), data)
                            except InvalidRequest as e:
                                self.logger.writeWarning("Error registering {} {}: {}".format(res_type, res_key, e))
                                self.logger.writeWarning("Request data: {}".format(data))
                                del self._registered["entities"][namespace][res_type][res_key]

                    elif queue_item["method"] == "DELETE":
                        translated_type = res_type + 's'
                        try:
                            self._SEND("DELETE", "/{}/{}/{}".format(namespace, translated_type, res_key))
                        except InvalidRequest as e:
                            self.logger.writeWarning("Error deleting resource {} {}: {}"
                                                     .format(translated_type, res_key, e))
                    else:
                        self.logger.writeWarning("Method {} not supported for Registration API interactions"
                                                 .format(queue_item["method"]))
                except Exception:
                    self._registered["registered"] = False
                    if(self._mdns_updater is not None):
                        self._mdns_updater.P2P_disable()
        self.logger.writeDebug("Stopping HTTP queue processing thread")

    def _queue_request(self, method, namespace, res_type, key):
        """Queue a request to be processed. Handles all requests except initial Node POST which is done in
        _process_reregister"""
        self._reg_queue.put({"method": method, "namespace": namespace, "res_type": res_type, "key": key})

    def register(self, res_type, key, **kwargs):
        """Register 'resource' type data including the Node
        NB: Node registration is managed by heartbeat thread so may take up to 5 seconds"""
        self.register_into("resource", res_type, key, **kwargs)

    def unregister(self, res_type, key):
        """Unregister 'resource' type data including the Node"""
        self.unregister_from("resource", res_type, key)

    def register_into(self, namespace, res_type, key, **kwargs):
        """General register method for 'resource' types"""
        data = kwargs
        send_obj = {"type": res_type, "data": data}
        if 'id' not in send_obj["data"]:
            self.logger.writeWarning("No 'id' present in data, using key='{}': {}".format(key, data))
            send_obj["data"]["id"] = key

        if namespace == "resource" and res_type == "node":
            # Handle special Node type
            self._registered["node"] = send_obj
            # Register with Auth server as Auth client
            self.register_auth_client(send_obj)
        else:
            self._add_mirror_keys(namespace, res_type)
            self._registered["entities"][namespace][res_type][key] = send_obj
        self._queue_request("POST", namespace, res_type, key)

    def unregister_from(self, namespace, res_type, key):
        """General unregister method for 'resource' types"""
        if namespace == "resource" and res_type == "node":
            # Handle special Node type
            self._registered["node"] = None
        elif res_type in self._registered["entities"][namespace]:
            self._add_mirror_keys(namespace, res_type)
            if key in self._registered["entities"][namespace][res_type]:
                del self._registered["entities"][namespace][res_type][key]
        self._queue_request("DELETE", namespace, res_type, key)

    def _add_mirror_keys(self, namespace, res_type):
        """Deal with missing keys in local mirror"""
        if namespace not in self._registered["entities"]:
            self._registered["entities"][namespace] = {}
        if res_type not in self._registered["entities"][namespace]:
            self._registered["entities"][namespace][res_type] = {}

    def _process_reregister(self):
        """Re-register just the Node, and queue requests in order for other resources"""
        if self._registered.get("node", None) is None:
            self.logger.writeDebug("No node registered, re-register returning")
            return

        try:
            self.logger.writeDebug("Clearing old Node from API prior to re-registration")
            self._SEND("DELETE", "/resource/nodes/" + self._registered["node"]["data"]["id"])
        except InvalidRequest as e:
            # 404 etc is ok
            self.logger.writeInfo("Invalid request when deleting Node prior to registration: {}".format(e))
        except Exception as e:
            # Server error is bad, no point continuing
            self.logger.writeError("Aborting Node re-register! {}".format(e))
            return

        self._registered["registered"] = False
        if(self._mdns_updater is not None):
            self._mdns_updater.inc_P2P_enable_count()

        # Drain the queue
        while not self._reg_queue.empty():
            try:
                self._reg_queue.get(block=False)
            except gevent.queue.Queue.Empty:
                break

        try:
            # Register the node, and immediately heartbeat if successful to avoid race with garbage collect.
            self.logger.writeInfo("Attempting re-registration for Node {}"
                                  .format(self._registered["node"]["data"]["id"]))
            self._SEND("POST", "/resource", self._registered["node"])
            self._SEND("POST", "/health/nodes/" + self._registered["node"]["data"]["id"])
            self._registered["registered"] = True
            if self._mdns_updater is not None:
                self._mdns_updater.P2P_disable()
        except Exception as e:
            self.logger.writeWarning("Error re-registering Node: {}".format(e))
            self.aggregator = ""  # Fallback to prevent us getting stuck if the Reg API issues a 4XX error incorrectly
            return

        # Re-register items that must be ordered
        # Re-register things we have in the local cache.
        # "namespace" is e.g. "resource"
        # "entities" are the things associated under that namespace.
        for res_type in self.registration_order:
            for namespace, entities in self._registered["entities"].items():
                if res_type in entities:
                    self.logger.writeInfo("Ordered re-registration for type: '{}' in namespace '{}'"
                                          .format(res_type, namespace))
                    for key in entities[res_type]:
                        self._queue_request("POST", namespace, res_type, key)

        # Re-register everything else
        # Re-register things we have in the local cache.
        # "namespace" is e.g. "resource"
        # "entities" are the things associated under that namespace.
        for namespace, entities in self._registered["entities"].items():
            for res_type in entities:
                if res_type not in self.registration_order:
                    self.logger.writeInfo("Unordered re-registration for type: '{}' in namespace '{}'"
                                          .format(res_type, namespace))
                    for key in entities[res_type]:
                        self._queue_request("POST", namespace, res_type, key)

    # Stop the Aggregator object running
    def stop(self):
        self.logger.writeDebug("Stopping aggregator proxy")
        self._running = False
        self.heartbeat_thread.join()
        self.queue_thread.join()

    def status(self):
        return {"api_href": self.aggregator,
                "api_version": AGGREGATOR_APIVERSION,
                "registered": self._registered["registered"]}

    def _get_api_href(self):
        protocol = "http"
        if _config.get('https_mode') == "enabled":
            protocol = "https"
        api_href = self.mdnsbridge.getHref(REGISTRATION_MDNSTYPE, None, AGGREGATOR_APIVERSION, protocol)
        if api_href == "":
            api_href = self.mdnsbridge.getHref(LEGACY_REG_MDNSTYPE, None, AGGREGATOR_APIVERSION, protocol)
        return api_href

    def register_auth_client(self, node_object):
        """Function for Registering OAuth client with Auth Server and instantiating OAuth Client class"""

        if OAUTH_MODE is True:
            client_name = node_object['data']['description']
            client_uri = 'http://' + node_object['data']['label']
            if self.auth_registrar is None:
                self.auth_registrar = self._auth_register(
                    client_name=client_name,
                    client_uri=client_uri
                )
            if self._registered['auth_client_registered'] and self.auth_client is None:
                try:
                    # Register Node Client
                    self.auth_registry.register_client(client_name=client_name, client_uri=client_uri)
                except (OSError, IOError):
                    self.logger.writeError("Exception accessing OAuth credentials. Could not register OAuth2 client.")
                    return
                # Extract the 'RemoteApp' class created when registering
                self.auth_client = getattr(self.auth_registry, client_name)
                # Fetch Token
                self.fetch_auth_token()

    def fetch_auth_token(self):
        """Fetch Access Token either using redirection grant flow or using auth_client"""
        if self.auth_client is not None and self.auth_registrar is not None:
            try:
                if "authorization_code" in self.auth_registrar.allowed_grant:
                    # Open browser at endpoint for redirecting to Auth Server's /authorize endpoint
                    webbrowser.open("http://" + getfqdn() + NODE_APIROOT + "login")
                elif "client_credentials" in self.auth_registrar.allowed_grant:
                    # Fetch Token
                    token = self.auth_client.fetch_access_token()
                    # Store token in member variable to be extracted using `fetch_local_token` function
                    self.auth_registry.bearer_token = token
                else:
                    raise OAuth2Error("Client registered with unsupported Grant Type")
            except OAuth2Error as e:
                self.logger.writeError("Failure fetching access token. {}".format(e))

    def _auth_register(self, client_name, client_uri):
        """Register OAuth client with Authorization Server"""
        auth_registrar = AuthRegistrar(
            client_name=client_name,
            redirect_uri='http://' + getfqdn() + NODE_APIROOT + 'authorize',
            client_uri=client_uri,
            allowed_scope=ALLOWED_SCOPE,
            allowed_grant=ALLOWED_GRANTS
        )
        if auth_registrar.registered is True:
            self._registered['auth_client_registered'] = True
            return auth_registrar

    def _SEND(self, method, url, data=None):
        """Handle sending all requests to the Registration API, and searching for a new 'aggregator' if one fails"""

        headers = {}

        if self.aggregator == "":
            self.aggregator = self._get_api_href()

        if data is not None:
            data = json.dumps(data)
            headers.update({"Content-Type": "application/json"})

        url = AGGREGATOR_APIROOT + AGGREGATOR_APIVERSION + url
        for i in range(0, 3):
            if self.aggregator == "":
                self.logger.writeWarning("No aggregator available on the network or mdnsbridge unavailable")
                raise NoAggregator(self._mdns_updater)

            self.logger.writeDebug("{} {}".format(method, urljoin(self.aggregator, url)))

            # We give a long(ish) timeout below, as the async request may succeed after the timeout period
            # has expired, causing the node to be registered twice (potentially at different aggregators).
            # Whilst this isn't a problem in practice, it may cause excessive churn in websocket traffic
            # to web clients - so, sacrifice a little timeliness for things working as designed the
            # majority of the time...
            try:
                kwargs = {
                    "method": method, "url": urljoin(self.aggregator, url),
                    "data": data, "timeout": 1.0, "headers": headers
                }
                if _config.get('prefer_ipv6') is True:
                    kwargs["proxies"] = {'http': ''}

                # If not in OAuth mode, perform standard request
                if OAUTH_MODE is False or self.auth_client is None:
                    R = requests.request(**kwargs)
                else:
                    # If in OAuth Mode, use OAuth client to automatically fetch token / refresh token if expired
                    with self.auth_registry.app.app_context():
                        try:
                            R = self.auth_client.request(**kwargs)
                        except InvalidTokenError:
                            self.logger.writeWarning("Invalid Token. Requesting new Token.")
                            self.fetch_auth_token()
                            try:
                                R = self.auth_client.request(**kwargs)  # Resend the request
                            except Exception as e:
                                self.logger.writeError("Error re-requesting token: {}. Removing Auth Client".format(e))
                                self.auth_client = None
                        except OAuth2Error as e:
                            self.logger.writeError("Failed to fetch token before making API call. Error: {}".format(e))

                if R is None:
                    # Try another aggregator
                    self.logger.writeWarning("No response from aggregator {}".format(self.aggregator))

                elif R.status_code in [200, 201]:
                    if R.headers.get("content-type", "text/plain").startswith("application/json"):
                        return R.json()
                    else:
                        return R.content

                elif R.status_code == 204:
                    return

                elif (R.status_code // 100) == 4:
                    self.logger.writeWarning("{} response from aggregator: {} {}"
                                             .format(R.status_code, method, urljoin(self.aggregator, url)))
                    raise InvalidRequest(R.status_code, self._mdns_updater)

                else:
                    self.logger.writeWarning("Unexpected status from aggregator {}: {}, {}"
                                             .format(self.aggregator, R.status_code, R.content))

            except requests.exceptions.RequestException as ex:
                # Log a warning, then let another aggregator be chosen
                self.logger.writeWarning("{} from aggregator {}".format(ex, self.aggregator))

            # This aggregator is non-functional
            self.aggregator = self._get_api_href()
            self.logger.writeInfo("Updated aggregator to {} (try {})".format(self.aggregator, i))

        raise TooManyRetries(self._mdns_updater)


class MDNSUpdater(object):
    def __init__(self, mdns_engine, mdns_type, mdns_name, mappings, port, logger, p2p_enable=False, p2p_cut_in_count=5,
                 txt_recs=None):
        self.mdns = mdns_engine
        self.mdns_type = mdns_type
        self.mdns_name = mdns_name
        self.mappings = mappings
        self.port = port
        self.service_versions = {}
        self.txt_rec_base = {}
        if txt_recs:
            self.txt_rec_base = txt_recs
        self.logger = logger
        self.p2p_enable = p2p_enable
        self.p2p_enable_count = 0
        self.p2p_cut_in_count = p2p_cut_in_count

        for mapValue in itervalues(self.mappings):
            self.service_versions[mapValue] = 0

        self.mdns.register(self.mdns_name, self.mdns_type, self.port, self.txt_rec_base)

        self._running = True
        self._mdns_update_queue = gevent.queue.Queue()
        self.mdns_thread = gevent.spawn(self._modify_mdns)

    def _modify_mdns(self):
        while self._running:
            if self._mdns_update_queue.empty():
                gevent.sleep(0.2)
            else:
                try:
                    txt_recs = self._mdns_update_queue.get()
                    self.mdns.update(self.mdns_name, self.mdns_type, txt_recs)
                except ServiceNotFoundException:
                    self.logger.writeError("Unable to update mDNS record of type {} and name {}"
                                           .format(self.mdns_name, self.mdns_type))

    def stop(self):
        self._running = False
        self.mdns_thread.join()

    def _p2p_txt_recs(self):
        txt_recs = self.txt_rec_base.copy()
        txt_recs.update(self.service_versions)
        return txt_recs

    def update_mdns(self, type, action):
        if self.p2p_enable:
            if (action == "register") or (action == "update") or (action == "unregister"):
                self.logger.writeDebug("mDNS action: {} {}".format(action, type))
                self._increment_service_version(type)
                self._mdns_update_queue.put(self._p2p_txt_recs())

    def _increment_service_version(self, type):
        self.service_versions[self.mappings[type]] = self.service_versions[self.mappings[type]] + 1
        if self.service_versions[self.mappings[type]] > 255:
            self.service_versions[self.mappings[type]] = 0

    # Counts up a number of times, and then enables P2P
    def inc_P2P_enable_count(self):
        if not self.p2p_enable:
            self.p2p_enable_count += 1
            if self.p2p_enable_count >= self.p2p_cut_in_count:
                self.P2P_enable()

    def _reset_P2P_enable_count(self):
        self.p2p_enable_count = 0

    def P2P_enable(self):
        if not self.p2p_enable:
            self.logger.writeInfo("Enabling P2P Discovery")
            self.p2p_enable = True
            self._mdns_update_queue.put(self._p2p_txt_recs())

    def P2P_disable(self):
        if self.p2p_enable:
            self.logger.writeInfo("Disabling P2P Discovery")
            self.p2p_enable = False
            self._reset_P2P_enable_count()
            self._mdns_update_queue.put(self.txt_rec_base)
        else:
            self._reset_P2P_enable_count()


if __name__ == "__main__":  # pragma: no cover
    from uuid import uuid4

    agg = Aggregator()
    ID = str(uuid4())

    agg.register("node", ID, id=ID, label="A Test Service", href="http://127.0.0.1:12345/", services=[], caps={},
                 version="0:0", hostname="apiTest")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agg.unregister("node", ID)
        agg.stop()
