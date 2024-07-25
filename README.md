# Flux

## Local Setup

### Prerequisites

- **Python 3.10.1**: This project requires Python 3.10.1. You can download it [here](https://www.python.org/downloads/release/python-3101/).

### Installation

1. **Install `pipenv`**: Pipenv is used for dependency management. Install it using the following command:

   ```sh
   python3.10 -m pip install --user pipenv
   ```

2. **Install Project Dependencies**: Navigate to the root directory of the project and run:

   ```sh
   pipenv install
   ```

### Running the Server

1. **Set the `PYTHONPATH`**: Ensure the `PYTHONPATH` is set to the root of your project directory.

   For Windows:

   ```sh
   set PYTHONPATH=F:\flux_project
   ```

   For Unix-based systems:

   ```sh
   export PYTHONPATH=/path/to/flux_project
   ```

2. **Start the Server**: Use the following command to run the server:

   ```sh
   pipenv run python -m src.server.server
   ```

### Running the Client

1. **Start the Client**: Use the following command to run the client:

   ```sh
   pipenv run python -m src.client.client
   ```
