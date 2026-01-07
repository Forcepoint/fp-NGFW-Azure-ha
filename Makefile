include Makefile.env

PIPENV=PIPENV_VENV_IN_PROJECT=yes pipenv
BROWSER?=firefox # to view ut coverage result
PYTHON=python3
ZIPAPP=$(PYTHON) -mzipapp --compress
GEN_INSTALL_SCRIPT=./utils/generate-script-installer.py


ZIP_SCRIPT=$(BUILD_PATH)/ha_script.pyz


# Standard targets

.PHONY: help
help: ## Print this help message
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort

.PHONY: all
all: init lint unit-test build-doc build-dist ## lint and build
	@printf "\n\033[36m%-25s\033[0m %s\n" "- The script to deploy is available: $(SCRIPT_INSTALLER_TARGET)"
	@printf "\033[36m%-25s\033[0m %s\n" "- The user doc is available: doc/user_guide.pdf"

.PHONY: unit-test
unit-test: $(VIRTUAL_ENV_PATH) ## Run unit tests
	$(PIPENV) run pytest --cov

.PHONY: unit-test-cov
unit-test-cov: unit-test ## Run unit tests and show coverage html report
	$(BROWSER) htmlcov/index.html

# Python #
.PHONY: init
init: $(VIRTUAL_ENV_PATH) ## Initialise main Python virtual environment
$(VIRTUAL_ENV_PATH):
	$(PIPENV) sync --dev

.PHONY: format
format: $(VIRTUAL_ENV_PATH) ## Format Python
	$(PIPENV) run isort $(SRC_PATH)
	$(PIPENV) run black $(SRC_PATH)

.PHONY: lint ## Lint Python
lint: lint-flake8
# temporary removing lint-ruff

.PHONY: lint-flake8
lint-flake8: $(VIRTUAL_ENV_PATH)
# echo "Entering directory '$(SRC_PATH)'"
	cd $(SRC_PATH) && $(PIPENV) run flake8
# echo "Leaving directory '$(SRC_PATH)'"

.PHONY: lint-ruff
lint-ruff: $(VIRTUAL_ENV_PATH)
	$(PIPENV) run ruff check $(SRC_PATH)

.PHONY: typing
typing: $(VIRTUAL_ENV_PATH) ## Type check Python
	echo "Entering directory '$(SRC_PATH)'"
	cd $(SRC_PATH) && $(PIPENV) run mypy --show-error-codes .
	echo "Leaving directory '$(SRC_PATH)'"

.PHONY: lock
lock: ## Create/Update the Pipfile lock file
	$(PIPENV) lock --dev

.PHONY: sync
sync: ## Update environments
	$(PIPENV) sync --dev


.PHONY: build-doc
build-doc: ## Build ha-script doc
	$(MAKE) -C doc pdf

.PHONY: build-dist
build-dist: ## Build installation script
	- rm -rf $(SRC_PATH)/*/*.pyc $(SRC_PATH)/*/__pycache__ $(SRC_PATH)/.*cache*
	test -d $(BUILD_PATH) || mkdir $(BUILD_PATH)
	$(ZIPAPP) -c $(SRC_PATH) -p "/usr/bin/env python3" -o $(ZIP_SCRIPT)
	$(GEN_INSTALL_SCRIPT) $(ZIP_SCRIPT) >$(SCRIPT_INSTALLER_TARGET)

.PHONY: clean
clean:
	- rm -rf $(BUILD_PATH) $(ZIPSCRIPT)
	- rm -rf $(VIRTUAL_ENV_PATH)
	- rm -rf $(SRC_PATH)/*/*.pyc $(SRC_PATH)/*/__pycache__
	- rm -rf .pytest_cache __pycache__ htmlcov .coverage unit_test_results.log coverage.xml
	- rm -rf $(SRC_PATH)/.mypy_cache .ruff_cache
