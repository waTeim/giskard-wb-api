# Docker image build/push and Helm install for the giskard-wb-api API server.
#
#   make config                                  # create config.mk from config.mk.example
#   make build                                   # builds <image>:<tag> locally
#   make push                                    # builds and pushes (REGISTRY must be set)
#   make push REGISTRY=ghcr.io/acme TAG=dev      # command-line values override config.mk
#   make install                                 # helm upgrade --install with values.yaml
#
# Settings are read from config.mk when present; see config.mk.example.

-include config.mk

DOCKER     ?= docker
REGISTRY   ?=
IMAGE_NAME ?= giskard-wb-api
VERSION    := $(or $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1),0.0.0)
TAG        ?= $(VERSION)
TAG_LATEST ?= true
PLATFORM   ?= linux/amd64
BUILD_ARGS ?=

HELM       ?= helm
RELEASE    ?= giskard-wb-api
NAMESPACE  ?= giskard
VALUES     ?= values.yaml
HELM_ARGS  ?=

IMAGE := $(if $(strip $(REGISTRY)),$(strip $(REGISTRY))/$(IMAGE_NAME),$(IMAGE_NAME))
TAGS  := $(TAG) $(if $(filter true,$(TAG_LATEST)),latest)

.PHONY: help config build push check-registry install check-values

help:
	@echo "Targets:"
	@echo "  config  Create config.mk from config.mk.example (if missing)"
	@echo "  build   Build $(foreach t,$(TAGS),$(IMAGE):$(t)) for $(PLATFORM)"
	@echo "  push    Build, then push those tags (requires REGISTRY)"
	@echo "  install Install or upgrade Helm release $(RELEASE) in namespace $(NAMESPACE) with $(VALUES)"
	@echo "Variables (config.mk or command line): DOCKER, REGISTRY, IMAGE_NAME, TAG, TAG_LATEST, PLATFORM, BUILD_ARGS,"
	@echo "  HELM, RELEASE, NAMESPACE, VALUES, HELM_ARGS"

# Deliberately not a `config.mk:` file rule: make would then auto-create the
# included file on every invocation.
config:
	@if [ -e config.mk ]; then echo "config.mk already exists; not overwriting"; \
	else cp config.mk.example config.mk && echo "Created config.mk - edit it to set REGISTRY and other options."; fi

build:
	$(DOCKER) build --platform $(PLATFORM) $(BUILD_ARGS) $(foreach t,$(TAGS),-t $(IMAGE):$(t)) .

push: check-registry build
	$(foreach t,$(TAGS),$(DOCKER) push $(IMAGE):$(t) &&) true

check-registry:
	@test -n "$(strip $(REGISTRY))" || { echo "REGISTRY is not set: add it to config.mk or run make push REGISTRY=ghcr.io/your-org" >&2; exit 1; }

# Fetches the chart's dependencies (as pinned in chart/Chart.lock, registering
# the SkyPilot Helm repository if needed), then
# installs or upgrades the release. When REGISTRY is set, the image is the one
# `make push` publishes ($(IMAGE):$(TAG)), overriding image.* in $(VALUES).
install: check-values
	@$(HELM) repo list 2>/dev/null | grep -q 'helm.skypilot.co' || $(HELM) repo add skypilot https://helm.skypilot.co
	$(HELM) dependency build chart
	$(HELM) upgrade --install $(RELEASE) ./chart --n $(NAMESPACE) -f $(VALUES) $(if $(strip $(REGISTRY)),--set image.repository=$(IMAGE) --set image.tag=$(TAG)) $(HELM_ARGS)

check-values:
	@test -f "$(VALUES)" || { echo "$(VALUES) not found: cp values.yaml.example $(VALUES) and edit it, or set VALUES=path" >&2; exit 1; }
