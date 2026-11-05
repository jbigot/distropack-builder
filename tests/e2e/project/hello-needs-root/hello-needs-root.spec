Name:           hello-needs-root
Version:        1.0
Release:        1%{?dist}
Summary:        Package of the distropack-builder test fixture that must fail
License:        MIT
BuildArch:      noarch

%description
Its build writes into /usr, which only root can do.

%build
touch /usr/share/hello-needs-root-stamp

%install
mkdir -p %{buildroot}/usr/share/hello-needs-root

%files
/usr/share/hello-needs-root

%changelog
* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1
- Initial release.
