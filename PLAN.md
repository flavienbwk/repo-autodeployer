# Plan prompts

## 1. Base project with docker-in-docker

You are a senior software engineer and architect. We're building a backend system that automates the process of deploying applications based on a natural language comment and a GitHub repository URL as input.

Create a FastAPI Python API that accepts 2 arguments as POST /request route input : a natural language description of deployment requirements and a GitHub link to a repository.

After processing a request, the API must produce a Terraform code that will be executed (inside the API) to deploy the repository code to an AWS EC2 VM. Your approach should be as generalizable as possible, and should not overfit one application type. That's why after cloning the repo provided to a temp folder, you will add a Docker and docker compose configuration to deploy it. Expose app port on host port 8080.

We will always use the same deployment method : API will trigger the provisioning of a t2.small EC2 VM provisioned in ca-central-1 on Ubuntu 24.04 through Terraform (authenticated through environments variables provided in the API), install the latest Docker version, transfer temp folder files (including repo files) and run `make up` to start the service.

Requirements for the API /request route :

- Clone the repo (with errors handling and proper logging system as stdout using a logging library) to a temp folder
- Retrieve and analyse the repo file tree (max depth 4)
- Parse the natural language description input and file tree to understand/deduce deployment requirements
- Deny the request if the repo is deploying something else than an HTTP-accessible endpoint/server
- Generate an LLM prompt to ask OpenAI API to generate all required Terraform files to deploy the repo
  - To determine how to architect your Docker configuration, first determine which files might contain the interesting information (e.g., app.py for determining the base exposed IP and port) and analyse them to extract the desired informations (exposed port).
- If project already implements Docker, rewrite the Docker configuration with compose to make it work as stated by the current requirements.
- Provide logs to stdout detailing the process, including provisioning, deployment, and any adjustments made.

For the requirement above, make sure to call an OpenAI API with credentials and model provided as env variables to process and get the desired informations.

What the API must also implement:

- A .env.example file with all desired env variables
- The API must implement a queuing system with a configurable amount of jobs (provided through env variable)
- It must implement a POST /request (processes the query as per the requirements), GET /list (returns the list of requested jobs and their status) and the GET /job/\<id> (returns the jobs infos including job ID; status and any log created) routes
- No authentication is required to request the API

The API must be Dockerized and have a Compose configuration. It should also have a Makefile and be runnable with `make up`.

Add a markdownlint GitHub Actions CI with default rules as a YAML file.

## 2. Scaling the system

Use an external control-plane/workers approach with a RabbitMQ or Celery system. Put all job logs to a PostgreSQL database to not lose job details. Update the API to reflect the possibility to also retrieve job logs from /job/\<id>.

## 3. Add recursive and versatile in-code analysis

Analyze the code repository to identify changes that need to be made. Here are some hints of what to look at: application type (e.g., Flask, Node.js, django…), dependencies and configurations (e.g., requirements.txt, package.json…), any necessary changes, such as updating environment variables or network settings.

For instance, some apps may require to set env variables as per their documentation or codebase. Make sure to analyse the code to determine how to setup the project into a container. You may need to use docker-in-docker if the project is already dockerized.

The script must call the LLM endpoint as many time as possible with the chosen files content to determine actions to do.

## 4. Determination of best deployment option

Instead of using a VM, automatically determine the type and configuration of deployment that would work best for the application. Should it be deployed on serverless infrastructure? Should it be deployed using kubernetes? A virtual machine.?

Update the part of the code that generates the final Terraform code

## 5. Healthchecks

After deployment, test if API is working with healthchecks...

## 6. Private repo support

Through PAT fine grained...

## 7. Improve security

Isolate AWS resources in dedicated VPCs and security groups...
