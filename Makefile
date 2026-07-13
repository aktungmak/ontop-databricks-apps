PROFILE ?= DEFAULT

.PHONY: validate deploy-volume deploy-mappings deploy-app deploy run

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
