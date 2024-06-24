import logging
import select
import socket
import os
import threading

import msgpack

from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from exceptions import RequestException, ExceptionCode

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = "127.0.0.1"
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"

# Client settings
CLIENT_IP = "127.0.0.1"
CLIENT_SEND_PORT = 5672
CLIENT_RECV_PORT = 4322

# Get the username from the user
user_name = input("Enter username: ")

# Ensure the logs directory exists
log_directory = "./logs"
os.makedirs(log_directory, exist_ok=True)

# Configure logging
log_filename = f"{log_directory}/client_{user_name}_{CLIENT_IP}.log"
logging.basicConfig(
    filename=log_filename, level=logging.DEBUG
)

# client_recv_socket is the socket that listens for incoming connections
# client_send_socket is the socket that sends messages to the server
client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM) 
client_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM) 

client_send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_send_socket.bind((CLIENT_IP, CLIENT_SEND_PORT))
client_recv_socket.bind((CLIENT_IP, CLIENT_RECV_PORT))

client_send_socket.connect((SERVER_IP, SERVER_PORT))
client_recv_socket.listen(5)

# Encode the username and prepare the header
encoded_username = user_name.encode(ENCODING_FORMAT)
username_header = f"n{len(encoded_username):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
client_send_socket.send(username_header + encoded_username)

# receiving = False
connected = [client_recv_socket]

def handle_sending():
    global client_send_socket
    with patch_stdout():
        recipient_prompt = PromptSession("\nEnter recipient's username: ")
        while True:
            recipient_username = recipient_prompt.prompt()
            if recipient_username:
                # Encode the recipient's username and prepare the header
                encoded_recipient = recipient_username.encode(ENCODING_FORMAT)
                request_header = f"r{len(encoded_recipient):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                logging.debug(f"Sent packet {(request_header + encoded_recipient).decode(ENCODING_FORMAT)}")
                client_send_socket.send(request_header + encoded_recipient)
                
                # Read response type
                response_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                logging.log(level=logging.DEBUG, msg=f"Response type: {response_type}")
                
                # Read response length and response
                response_length = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
                response = client_send_socket.recv(response_length)
                
                if response_type == "r":
                    # If response type is 'r', unpack the recipient address
                    recipient_addr = response.decode(ENCODING_FORMAT)
                    logging.log(level=logging.DEBUG, msg=f"Response: {recipient_addr}")
                    
                    # Connect to the recipient's client socket
                    client_peer_socket = socket.socket(
                        socket.AF_INET, socket.SOCK_STREAM
                    )
                    client_peer_socket.connect((recipient_addr, CLIENT_RECV_PORT))
                    while True:
                        # Send message to the recipient
                        msg_prompt = PromptSession(
                            f"\nEnter message for {recipient_username.encode(ENCODING_FORMAT)}: "
                        )
                        msg = msg_prompt.prompt()
                        msg = msg.encode(ENCODING_FORMAT)
                        if msg == b"exit":
                            break
                        header = f"m{len(msg):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                        client_peer_socket.send(header + msg)

                elif response_type == "e":
                    # If response type is 'e', unpack the error message
                    error: RequestException = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
                    logging.log(level=logging.ERROR, msg=error)

def receive_message(socket: socket.socket) -> str:
    # Read message type
    message_type = socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    if not message_type:
        raise RequestException(
            msg=f"Peer at {socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    elif message_type != "m":
        raise RequestException(
            msg="Invalid message type in header",
            code=ExceptionCode.INVALID_HEADER,
        )
    else:
        # Read message length and message content
        message_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        return socket.recv(message_len).decode(ENCODING_FORMAT)

def handle_receiving():
    global client_send_socket, client_recv_socket, user_name
    # Read from the connected sockets
    while True:
        read_sockets, _, __ = select.select(connected, [], [], 0.1)
        for notified_socket in read_sockets:
            if notified_socket == client_recv_socket:
                # Accept new connection from peer
                peer_socket, peer_addr = client_recv_socket.accept()
                # peer_addr is {ip, port} of the incoming connection
                logging.log(
                    level=logging.DEBUG,
                    msg=(
                        "Accepted new connection from"
                        f" {peer_addr[0]}:{peer_addr[1]}"
                    ),
                )
                try:
                    connected.append(peer_socket)
                    #lookup is peer_addr[0] which is the ip address of the incoming connection
                    lookup: bytes = peer_addr[0].encode(ENCODING_FORMAT)
                    header = f"l{len(lookup):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                    logging.debug(f"Sending packet {(header + lookup).decode(ENCODING_FORMAT)}")
                    client_send_socket.send(header + lookup)
                    # Receive response type
                    res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                    if res_type not in ["l", "e"]:
                        raise RequestException(
                            msg="Invalid message type in header",
                            code=ExceptionCode.INVALID_HEADER,
                        )
                    else:
                        response_length = int(
                            client_send_socket.recv(HEADER_MESSAGE_LENGTH)
                            .decode(ENCODING_FORMAT)
                            .strip()
                        )
                        response = client_send_socket.recv(response_length)
                        if res_type == "l":
                            # Log the username of the incoming connection
                            user_name = response.decode(ENCODING_FORMAT)
                            print(f"User {user_name} is trying to send a message")
                        else:
                            exception = msgpack.unpackb(
                                response,
                                object_hook=RequestException.from_dict,
                                raw=False,
                            )
                            logging.error(exception)
                            raise exception
                except RequestException as e:
                    logging.log(level=logging.ERROR, msg=e)
                    break
            else:
                try:
                    # Receive and log the message
                    msg: str = receive_message(notified_socket)
                    print(f"{user_name} says: {msg}")

                except RequestException as e:
                    if e.code == ExceptionCode.DISCONNECT:
                        try:
                            connected.remove(notified_socket)
                        except ValueError:
                            logging.info("already removed")
                    logging.log(level=logging.ERROR, msg=f"Exception: {e.msg}")
                    break

def main():
    send_thread = threading.Thread(target=handle_sending)
    receive_thread = threading.Thread(target=handle_receiving)
    send_thread.start()
    receive_thread.start()
    
    # Wait for threads to finish
    send_thread.join()
    receive_thread.join()



if __name__ == "__main__":
    main()