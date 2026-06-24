BE=nolas

run:
	docker compose up

build:
	docker compose up --build

upgrade:
	docker exec -it $(BE) alembic upgrade head

revision:
	if [[ -z "$(file_name)" ]]; then \
		echo "Please provide a file name for the migration"; \
		echo "For example: make revision file_name=\"some_migration_name\""; \
	else docker exec -it $(BE) alembic revision --autogenerate -m "$(file_name)"; \
	fi

downgrade:
	docker exec -it $(BE) alembic downgrade -1

imap-debug-shell:
	docker exec -it $(BE) env PYTHONSTARTUP=app/debug/debug_startup.py python -m asyncio

test:
	@if [[ "$(test_name)" == "" ]]; then \
		if [ -t 1 ]; then \
			docker exec $(BE) pytest -x -W ignore tests/$$(echo "$(file)" | sed 's|^tests/||'); \
		else \
			docker exec $(BE) pytest -x -W ignore tests/$$(echo "$(file)" | sed 's|^tests/||'); \
		fi \
	else \
		if [ -t 1 ]; then \
			docker exec $(BE) pytest -W ignore tests/$$(echo "$(file)" | sed 's|^tests/||') -k $(test_name); \
		else \
			docker exec $(BE) pytest -W ignore tests/$$(echo "$(file)" | sed 's|^tests/||') -k $(test_name); \
		fi \
	fi