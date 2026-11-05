Name:           hello-undeclared
Version:        1.0
Release:        1%{?dist}
Summary:        Package of the distropack-builder test fixture that must fail
License:        MIT
BuildArch:      noarch

%description
Its build runs cmake, which it does not declare: present on the host, absent from the build environment.

%build
cmake --version

%install
mkdir -p %{buildroot}/usr/share/hello-undeclared

%files
/usr/share/hello-undeclared

%changelog
* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1
- Initial release.
