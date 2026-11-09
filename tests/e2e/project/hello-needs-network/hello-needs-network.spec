Name:           hello-needs-network
Version:        1.0
Release:        1%{?dist}
Summary:        Package of the distropack-builder test fixture that must fail
License:        MIT
BuildArch:      noarch

%description
Its build connects to the network, which the archives' builders do not have.

%build
bash -c 'exec 3<>/dev/tcp/deb.debian.org/80' || { echo 'hello-needs-network: no network'; exit 1; }

%install
mkdir -p %{buildroot}/usr/share/hello-needs-network

%files
/usr/share/hello-needs-network

%changelog
* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1
- Initial release.
