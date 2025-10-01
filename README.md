# Repo Autodeployer

An API that deploys any repo provided to the Cloud.

Currently supports HTTP-based services coded in Python, Node, Go or Java/Spring.

Also find:

- [The API description and architecture](./AGENT.md) ;
- [Future improvement plans](./PLAN.md).

## Pre-requisite

- An AWS account and AWS user keys [with the appropriate role](./AWS_POLICY.md) ;
- An OpenAI API key ;
- Docker and Compose >= 28.x.x.

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
    curl -X POST http://localhost:8000/request \
        -H "Content-Type: application/json" \
        -d '{"description":"Deploy this flask application","repo_url":"https://github.com/Arvo-AI/hello_world"}'
    ```

    Your app will be deployed on the public IP of the VM on port 8080

## Other route examples

- List jobs for the current session

    ```bash
    curl -X GET http://localhost:8000/list
    ```

- Get job details for the current session

    ```bash
    curl -X GET http://localhost:8000/job/<id>
    ```

## Features

- Versatile containerized deployments ;
- Auto-generated TF and Docker configuration ;
- Secure connectivity to EC2 intances through individual SSH keys ;
- Robust logging system ;
- Versatile (Docker) packaging ;
- Secure resources consumption and limits ;
- LLM failovers if LLM generates invalid TF files ;
- Preventing LLM prompts injection with auto-generated delimiters ;
- Queued multi-workers pool.
