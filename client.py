import logging
import socket

import msgpack

from exceptions import RequestException

# Configure logging
logging.basicConfig(filename="./client.log", level=logging.DEBUG)

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = "127.0.0.1"
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"

# Get the username from the user
user_name = input("Enter username: ")
client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((SERVER_IP, SERVER_PORT))

# Encode the username and prepare the header
encoded_username = user_name.encode(ENCODING_FORMAT)
username_header = f"n{len(encoded_username):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
client_socket.send(username_header + encoded_username)

while True:
    # Get the recipient's username from the user
    recipient_username = input("Enter recipient's username: ")
    if recipient_username:
        # Encode the recipient's username and prepare the header
        encoded_recipient = recipient_username.encode(ENCODING_FORMAT)
        request_header = f"r{len(encoded_recipient):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
        client_socket.send(request_header + encoded_recipient)

        response_type = client_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
        logging.log(level=logging.DEBUG, msg=f"Response type: {response_type}")

        response_length = int(client_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
        
        response = client_socket.recv(response_length)
        
        if response_type == "r":
            # Decode the response using MessagePack if it's a valid response
            response = msgpack.unpackb(response, use_list=False)
            logging.log(level=logging.DEBUG, msg=f"Response: {response}")
        elif response_type == "e":
            # Decode the error message using MessagePack if it's an error response
            error: RequestException = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
            logging.log(level=logging.ERROR, msg=error)
