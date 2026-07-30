DEPS := git libffi-dev libpq-dev libssl-dev
WEBDEPS := $(DEPS) lighttpd
ARCH := $(shell uname -m)
GOARCH := $(shell echo $(ARCH) | sed s/x86_64/amd64/ | sed s/aarch64/arm64/ | sed s/armv7l/armv6l/)
GODIST := go1.23.4.linux-$(GOARCH).tar.gz
HOME := /home/twcmanager
SUDO := sudo
USER := twcmanager
GROUP := twcmanager
VER := $(shell lsb_release -sr)
BLUETOOTH = $(shell grep -c bluetooth /etc/group)

# Venv configuration (overridable: make venv-install VENV_DIR=/opt/twcmanager/venv)
VENV_DIR := $(HOME)/venv
VENV_PYTHON := $(VENV_DIR)/bin/python3
VENV_PIP := $(VENV_DIR)/bin/pip3

# Detect whether pip supports --break-system-packages (pip >= 22.1, PEP 668 systems)
PIP_BREAK_FLAG := $(shell pip3 install --break-system-packages --dry-run pip 2>&1 | grep -q "unknown option\|no such option\|unrecognized" && echo "" || echo "--break-system-packages")

# pip invocation wrapper: use --break-system-packages where supported, plain pip elsewhere
define pip_install
	$(SUDO) pip3 install $(PIP_BREAK_FLAG) $(1)
endef

.PHONY: tests upload

build: deps build_pkg
docker: deps install_pkg config tesla-control
webbuild: webdeps build_pkg

arch:
	echo $(ARCH)
config:
	# Create twcmanager user and group
	$(SUDO) useradd -U -m $(USER) 2>/dev/null; exit 0
	$(SUDO) usermod -a -G dialout $(USER)
ifeq ($(BLUETOOTH),1)
	$(SUDO) usermod -a -G bluetooth $(USER)
endif
	# Create configuration directory
	$(SUDO) mkdir -p /etc/twcmanager
ifeq (,$(wildcard /etc/twcmanager/config.json))
	$(SUDO) cp etc/twcmanager/config.json /etc/twcmanager/
endif
	$(SUDO) chown $(USER):$(GROUP) /etc/twcmanager -R
	$(SUDO) chmod 755 /etc/twcmanager -R

deps:
	$(SUDO) apt-get update
	$(SUDO) apt-get install -y $(DEPS)

webdeps:
	$(SUDO) apt-get update

ifeq ($(VER), 9.11)
	$(SUDO) apt-get install -y $(WEBDEPS) php7.0-cgi
else ifeq ($(VER), stretch)
	$(SUDO) apt-get install -y $(WEBDEPS) php7.0-cgi
else ifeq ($(VER), 16.04)
	$(SUDO) apt-get install -y $(WEBDEPS) php7.0-cgi
else ifeq ($(VER), 16.10)
	$(SUDO) apt-get install -y $(WEBDEPS) php7.0-cgi
else ifeq ($(VER), 20.04)
	$(SUDO) apt-get install -y $(WEBDEPS) php7.4-cgi
else
	$(SUDO) apt-get install -y $(WEBDEPS) php7.3-cgi
endif
	$(SUDO) lighty-enable-mod fastcgi-php ; exit 0
	$(SUDO) service lighttpd force-reload ; exit 0

install: deps install_pkg config
webinstall: webdeps install_pkg config webfiles

tesla-control:
	mkdir -p $(HOME)/gobin
	cd $(HOME) && wget https://go.dev/dl/$(GODIST)
	cd $(HOME) && tar -xvf $(GODIST)
	rm $(HOME)/$(GODIST)
	echo "export GOPATH=$(HOME)/go" >> $(HOME)/.bashrc
	echo "export $$PATH:\$GOPATH/bin" >> $(HOME)/.bashrc
	git clone https://github.com/teslamotors/vehicle-command $(HOME)/vehicle-control || exit 0
	cd $(HOME)/vehicle-control && GOPATH=$(HOME)/go PATH=$(HOME)/go/bin:$$PATH go get ./...
	cd $(HOME)/vehicle-control && GOPATH=$(HOME)/go PATH=$(HOME)/go/bin:$$PATH go build ./...
	cd $(HOME)/vehicle-control && GOPATH=$(HOME)/go PATH=$(HOME)/go/bin:$$PATH GOBIN=$(HOME)/gobin go install ./...
	$(SUDO) setcap 'cap_net_raw,cap_net_admin+eip' $(HOME)/gobin/tesla-control

testconfig:
	# Create twcmanager user and group
	$(SUDO) useradd -U -M $(USER); exit 0

	# Create configuration directory
	$(SUDO) mkdir -p /etc/twcmanager
ifeq (,$(wildcard /etc/twcmanager/config.json))
	$(SUDO) cp etc/twcmanager/.testconfig.json /etc/twcmanager/config.json
endif
	$(SUDO) chown $(USER):$(GROUP) /etc/twcmanager -R
	$(SUDO) chmod 755 /etc/twcmanager -R

build_pkg:
	# Install build pre-requisite
	$(SUDO) apt-get -y install python3-venv

	# Install TWCManager packages
ifeq ($(CI), 1)
	$(SUDO) /home/docker/.pyenv/shims/pip3 install -r requirements.txt
	$(SUDO) /home/docker/.pyenv/shims/python3 -m build
else
ifneq (,$(wildcard /usr/bin/pip3))
	$(call pip_install,--upgrade pip)
	$(call pip_install,--upgrade setuptools)
	$(call pip_install,-r requirements.txt)
else
ifneq (,$(wildcard /usr/bin/pip))
	$(SUDO) pip install --upgrade pip
	$(SUDO) pip install --upgrade setuptools
	$(SUDO) pip install -r requirements.txt
endif
endif
	$(SUDO) python3 -m build
endif

install_pkg:
ifneq (,$(wildcard /usr/bin/pip3))
	$(call pip_install,--upgrade pip)
	$(call pip_install,--upgrade setuptools)
	$(call pip_install,-r requirements.txt)
	$(call pip_install,.)
else
ifneq (,$(wildcard /usr/bin/pip))
	$(SUDO) pip install --upgrade pip
	$(SUDO) pip install --upgrade setuptools
	$(SUDO) pip install -r requirements.txt
	$(SUDO) pip install .
endif
endif

# Install into a virtual environment (does not affect system Python).
# By default the venv is created at $(VENV_DIR) ($(HOME)/venv).
# Override with: make venv-install VENV_DIR=/path/to/venv
#
# After running this target, update the systemd service ExecStart to use
# the venv interpreter:
#   ExecStart=$(VENV_DIR)/bin/python3 -u -m TWCManager.TWCManager
venv-install:
	$(SUDO) apt-get -y install python3-venv
	$(SUDO) -u $(USER) python3 -m venv $(VENV_DIR)
	$(SUDO) -u $(USER) $(VENV_PIP) install --upgrade pip setuptools wheel
	$(SUDO) -u $(USER) $(VENV_PIP) install -r requirements.txt
	$(SUDO) -u $(USER) $(VENV_PIP) install .
	@echo ""
	@echo "TWCManager installed into venv at $(VENV_DIR)."
	@echo "To use this venv with the systemd service, set:"
	@echo "  ExecStart=$(VENV_DIR)/bin/python3 -u -m TWCManager.TWCManager"
	@echo "in /etc/systemd/system/twcmanager.service, then run:"
	@echo "  sudo systemctl daemon-reload && sudo systemctl restart twcmanager"

# Build package inside a local .venv (for development / testing the venv install path).
# Creates .venv in the repo directory, does not require root.
venv-build:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip setuptools wheel
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install -r requirements-test.txt
	.venv/bin/pip install -e .
	@echo ""
	@echo "Development venv ready at .venv/"
	@echo "Activate with: source .venv/bin/activate"

test_direct:
	cd tests && make test_direct

test_service:
	cd tests && make test_service

test_service_nofail:
	cd tests && make test_service_nofail

tests:
	cd tests && make

upload:
	cd tests && make upload

webfiles:
	$(SUDO) cp html/* /var/www/html/
	$(SUDO) chown -R www-data:www-data /var/www/html
	$(SUDO) chmod -R 755 /var/www/html
	$(SUDO) usermod -a -G www-data $(USER)
