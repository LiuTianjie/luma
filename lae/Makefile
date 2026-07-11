UV ?= uv
PYTHON ?= $(UV) run --package lae-agent-runner python
CONTRACT_PYTHONPATH := packages/contracts/src
COMPONENT_PYTHONPATH := packages/contracts/src:packages/python/lae-agent-core/src:packages/python/lae-core/src:packages/python/lae-luma-adapter/src:packages/python/lae-store/src:apps/api/src:services/worker/src:services/agent-controller/src:services/agent-runner/src:cli/src

.PHONY: contracts test compile smoke check

contracts:
	PYTHONPATH=$(CONTRACT_PYTHONPATH) $(PYTHON) -m lae_contracts validate

test:
	PYTHONPATH=$(CONTRACT_PYTHONPATH) $(PYTHON) -m unittest discover -s tests -p 'test_*.py'

compile:
	$(PYTHON) -m compileall -q apps cli packages services tests

smoke:
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_api --health
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_worker --once
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_agent_controller --health
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_agent_runner --health
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_agent_runner --run < packages/contracts/src/lae_contracts/specs/examples/valid/luma-builder-task.analyze.json
	PYTHONPATH=$(COMPONENT_PYTHONPATH) $(PYTHON) -m lae_cli --format json version

check: contracts test compile smoke
