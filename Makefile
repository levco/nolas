BE=nolas

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
