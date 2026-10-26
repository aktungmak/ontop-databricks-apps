PROFILE ?= DEFAULT

.PHONY: validate deploy-volume deploy-mappings deploy-app deploy run app-test shacl-test shacl-it shacl-it-destroy ui-test destroy-app destroy-mappings destroy-volume destroy

validate:
	databricks bundle validate --strict -t volume --profile $(PROFILE)
	databricks bundle validate --strict -t mappings --profile $(PROFILE)
	databricks bundle validate --strict -t app --profile $(PROFILE)

deploy-volume:
	databricks bundle deploy -t volume --profile $(PROFILE)

deploy-mappings:
	databricks bundle deploy -t mappings --profile $(PROFILE)

deploy-app:
	databricks bundle deploy -t app --profile $(PROFILE)

deploy: deploy-volume deploy-mappings deploy-app

run: deploy
	databricks bundle run ontop_vkg -t app --profile $(PROFILE)

app-test:
	cd src/app && python3 -m pytest tests/ -q

shacl-test:
	cd src/app && python3 -m pytest shacl/tests -m "not integration" -q

shacl-it:
	BUNDLE_VAR_instance=shacl-it $(MAKE) run PROFILE=$(PROFILE)
	python3 scripts/run-shacl-it.py --profile $(PROFILE)

shacl-it-destroy:
	BUNDLE_VAR_instance=shacl-it $(MAKE) destroy PROFILE=$(PROFILE)

ui-test:
	cd src/app/static/mapper && npm install --no-fund --no-audit && npm test

destroy-app:
	databricks bundle destroy -t app --auto-approve --profile $(PROFILE)

destroy-mappings:
	databricks bundle destroy -t mappings --auto-approve --profile $(PROFILE)

destroy-volume:
	databricks bundle destroy -t volume --auto-approve --profile $(PROFILE)

destroy: destroy-app destroy-mappings destroy-volume
