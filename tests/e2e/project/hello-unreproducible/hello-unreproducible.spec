Name:           hello-unreproducible
Version:        1.0
Release:        1%{?dist}
Summary:        Package of the distropack-builder test fixture that must fail
License:        MIT
BuildArch:      noarch

%description
Its build writes the time into its package, which no second build can reproduce.

%install
mkdir -p %{buildroot}/usr/share/hello-unreproducible
date +%%s%%N > %{buildroot}/usr/share/hello-unreproducible/stamp

%files
/usr/share/hello-unreproducible

%changelog
* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1
- Initial release.
