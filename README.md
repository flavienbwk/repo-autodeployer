# Repo Autodeployer

An API that deploys any repo provided to the Cloud.

## Pre-requisite

- An AWS account and AWS user keys ;
- Docker and Compose >= 28.x.x

## Getting started

1. **Copy** and **edit** env variables

    ```bash
    cp .env.example .env
    ```

2. **Run** the API

    ```bash
    docker compose up --build -d
    ```

3. **Run** a first query

    ```bash
    curl -X POST http://localhost:8333/request \
        -H "Content-Type: application/json" \
        -d '{"description":"Your description here","repo_url":"https://github.com/Arvo-AI/hello_world"}'
    ```
