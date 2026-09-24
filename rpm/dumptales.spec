Name:           dumptales
Version:        0.1.2
Release:        1%{?dist}
Summary:        Explain row and relationship changes between database snapshots
License:        MIT
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-poetry-core
BuildRequires:  python3-wheel
BuildRequires:  python3-pytest
BuildRequires:  gcc
Requires:       python3 >= 3.10

%description
Compare MySQL and PostgreSQL text dumps and SQLite database files offline.
Reports row and schema changes in human-readable, JSON or JSONL form.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install

%check
%pytest

%files
%license LICENSE
%doc README.md
%{_bindir}/dumptales
%{python3_sitearch}/dumptales.py
%{python3_sitearch}/dialect_rows.py
%{python3_sitearch}/flat_backend.py
%{python3_sitearch}/fast_skip.py
%{python3_sitearch}/snapshot.py
%{python3_sitearch}/__pycache__/*
%{python3_sitearch}/_dumptales_native*.so
%{python3_sitearch}/dumptales-*.dist-info/

%changelog
* Thu Sep 24 2026 Miguel Jacq <mig@mig5.net> - 0.1.2-1
- Detect dialect and compression automatically.
- Fix postgres primary key detection
* Thu Sep 24 2026 Miguel Jacq <mig@mig5.net> - 0.1.1-1
- Fix for Pypi.
* Thu Sep 24 2026 Miguel Jacq <mig@mig5.net> - 0.1.0-1
- Initial public packaging.
