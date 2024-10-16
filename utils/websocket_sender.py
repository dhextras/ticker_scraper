import json

import websockets


async def send_ws_message(message, ws_server_url):
    """
    Sends a message to a WebSocket server.

    :param message: The message to send (will be converted to JSON)
    :param ws_server_url: The URL of the WebSocket server
    """
    if not ws_server_url:
        raise ValueError("WebSocket server URL must be provided.")

    async with websockets.connect(ws_server_url) as websocket:
        await websocket.send(json.dumps(message))
