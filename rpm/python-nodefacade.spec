Name: 			nmosnode
Version: 		0.1.0
Release: 		1%{?dist}
License: 		Internal Licence
Summary: 		Provides the ipstudio node facade service

Source0: 		%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:	python2-devel
BuildRequires:  python-setuptools
BuildRequires:  nmoscommon
BuildRequires:	systemd

Requires:       python
Requires:       nmos-reverse-proxy
Requires:	nmoscommon
%{?systemd_requires}

%description
IS-04 node facade service

%prep
%setup -n %{name}-%{version}

%build
%{py2_build}

%install
%{py2_install}

# Install systemd unit file
install -D -p -m 0644 rpm/ips-nodefacade.service %{buildroot}%{_unitdir}/ips-nodefacade.service

# Install Apache config file
install -D -p -m 0644 rpm/ips-api-node.conf %{buildroot}%{_sysconfdir}/httpd/conf.d/ips-apis/ips-api-node.conf


%post
%systemd_post ips-nodefacade.service
systemctl start ips-nodefacade
systemctl reload httpd


%preun
systemctl stop ips-nodefacade

%clean
rm -rf %{buildroot}

%files
%{_bindir}/nmos-node

%{_unitdir}/ips-nodefacade.service

%{python2_sitelib}/nodefacade
%{python2_sitelib}/nmosnodefacade-%{version}*.egg-info

%defattr(-,ipstudio, ipstudio,-)
%config %{_sysconfdir}/httpd/conf.d/nmos-apis/nmos-api-node-v1_0.conf

%changelog
* Fri Nov 10 2017 Simon Rankine <Simon.Rankine@bbc.co.uk> - 0.1.0-2
- Re-packaging for open sourcing
* Tue Apr 25 2017 Sam Nicholson <sam.nicholson@bbc.co.uk> - 0.1.0-1
- Initial packaging for RPM
