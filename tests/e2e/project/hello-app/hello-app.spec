Name:           hello-app
Version:        1.0
Release:        1%{?dist}
Summary:        Application of the distropack-builder test fixture
License:        MIT
BuildArch:      noarch
BuildRequires:  hello-lib

%description
Built with the hello function of hello-lib, which it build-requires.

%build
. /usr/share/hello-lib/hello.sh && hello > greeting

%install
install -D -m 644 greeting %{buildroot}/usr/share/hello-app/greeting

%files
/usr/share/hello-app/greeting

%changelog
* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1
- Initial release.
