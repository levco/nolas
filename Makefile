BE=nolas

upgrade:
	docker exec -it $(BE) alembic upgrade head

migrate:
	if [[ -z "$(msg)" ]]; then \
		echo "Please provide a message for the migration"; \
		echo "For example: make migrate msg=\"some migration name\""; \
	else docker exec -it $(BE) alembic revision --autogenerate -m "$(msg)"; \
	fi

downgrade:
	docker exec -it $(BE) alembic downgrade -1
