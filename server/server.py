"""
NeuraX Signaling Server

Purpose:
    Flask-SocketIO signaling server for WebRTC peer-to-peer connection setup.
    Relays SDP offers, answers, and ICE candidates between client and compute nodes
    without inspecting or storing the data.

Architecture:
    - Stateless relaying of WebRTC signaling messages
    - No persistent data storage (privacy-first design)
    - Supports multiple concurrent sessions via room-based messaging
    - CORS enabled for cross-origin connections
"""

import logging
import os
from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room

# Configure logging to track connection events
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Step 1: Enable CORS for all origins (prototype mode)
# In production, restrict to specific domains
CORS(app, resources={r"/*": {"origins": "*"}})

# Step 2: Initialize Socket.IO with async mode
# Eventlet async mode enables concurrent connections
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=True,
    engineio_logger=False
)

# Step 3: Track active sessions (volatile, not persisted)
# Maps session_id -> {client_id, compute_id}
active_sessions = {}


@app.route('/')
def health_check():
    """
    Health check endpoint for deployment monitoring.
    
    Returns:
        dict: Server status and connection info
    """
    return {
        "status": "online",
        "service": "neurax-signaling",
        "active_sessions": len(active_sessions)
    }


@socketio.on('connect')
def handle_connect():
    """
    Handle new WebSocket connection.
    
    Logs connection and emits ack to client.
    No authentication in prototype mode.
    """
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to NeuraX signaling server'})


@socketio.on('disconnect')
def handle_disconnect():
    """
    Handle WebSocket disconnection.
    
    Cleans up session tracking for departing peer.
    This prevents zombie sessions and potential memory leaks.
    """
    logger.info(f"Client disconnected: {request.sid}")
    
    # Step 1: Find and remove session entries with this client
    sessions_to_remove = []
    for session_id, session_data in active_sessions.items():
        if session_data.get('client_id') == request.sid or \
           session_data.get('compute_id') == request.sid:
            sessions_to_remove.append(session_id)
    
    # Step 2: Clean up orphaned sessions
    for session_id in sessions_to_remove:
        del active_sessions[session_id]
        logger.debug(f"Removed session: {session_id}")


@socketio.on('create_session')
def handle_create_session(data):
    """
    Client creates a new WebRTC session.
    
    Expected data:
        - session_id: Unique identifier for this peer connection
        
    Creates room for client-compute communication.
    """
    session_id = data.get('session_id')
    if not session_id:
        emit('error', {'message': 'session_id required'})
        return
    
    # Step 1: Register client in session
    if session_id not in active_sessions:
        active_sessions[session_id] = {}
    
    active_sessions[session_id]['client_id'] = request.sid
    
    # Step 2: Join Socket.IO room for this session
    # Rooms enable broadcasting to all peers in a session
    join_room(session_id)
    
    logger.info(f"Session created: {session_id} by client {request.sid}")
    emit('session_created', {'session_id': session_id})


@socketio.on('join_as_compute')
def handle_join_as_compute(data):
    """
    Compute node joins an existing session.
    
    Expected data:
        - session_id: Session to join
        
    Pairs client with compute node for peer-to-peer connection.
    """
    session_id = data.get('session_id')
    if not session_id:
        emit('error', {'message': 'session_id required'})
        return
    
    # Step 1: Check if session exists
    if session_id not in active_sessions:
        emit('error', {'message': 'Session not found'})
        return
    
    # Step 2: Register compute node
    active_sessions[session_id]['compute_id'] = request.sid
    
    # Step 3: Join session room
    join_room(session_id)
    
    logger.info(f"Compute node joined session: {session_id}")
    emit('joined', {'role': 'compute', 'session_id': session_id})


@socketio.on('offer')
def handle_offer(data):
    """
    Relay WebRTC SDP offer from client to compute node.
    
    Expected data:
        - session_id: Session identifier
        - offer: SDP offer string
        
    Why relay?
    - Peer discovery without exposing client IPs
    - NAT traversal via signaling path
    - No modification of SDP (transparent pass-through)
    """
    session_id = data.get('session_id')
    offer = data.get('offer')
    
    # Step 1: Validate inputs
    if not session_id or not offer:
        emit('error', {'message': 'session_id and offer required'})
        return
    
    # Step 2: Verify sender is client in this session
    if active_sessions.get(session_id, {}).get('client_id') != request.sid:
        emit('error', {'message': 'Unauthorized: not client for this session'})
        return
    
    # Step 3: Broadcast to session room (compute node will receive)
    # Using room so compute doesn't need to know client's socket ID
    socketio.emit('offer', {
        'session_id': session_id,
        'offer': offer,
        'from': request.sid
    }, room=session_id)
    
    logger.info(f"Offer relayed for session {session_id}")


@socketio.on('answer')
def handle_answer(data):
    """
    Relay WebRTC SDP answer from compute node to client.
    
    Expected data:
        - session_id: Session identifier
        - answer: SDP answer string
        
    Completes SDP exchange for WebRTC connection establishment.
    """
    session_id = data.get('session_id')
    answer = data.get('answer')
    
    # Step 1: Validate inputs
    if not session_id or not answer:
        emit('error', {'message': 'session_id and answer required'})
        return
    
    # Step 2: Verify sender is compute node in this session
    if active_sessions.get(session_id, {}).get('compute_id') != request.sid:
        emit('error', {'message': 'Unauthorized: not compute for this session'})
        return
    
    # Step 3: Relay answer to client
    socketio.emit('answer', {
        'session_id': session_id,
        'answer': answer,
        'from': request.sid
    }, room=session_id)
    
    logger.info(f"Answer relayed for session {session_id}")


@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    """
    Relay ICE candidate for NAT traversal.
    
    Expected data:
        - session_id: Session identifier
        - candidate: ICE candidate object
        - sdp_mid: SDP media stream ID
        - sdp_mline_index: SDP media line index
        
    Why relay ICE candidates?
    - Helps establish direct peer connection through firewalls
    - STUN/TURN servers provide public IPs and relay servers
    - Multiple candidates tried until connection succeeds
    """
    session_id = data.get('session_id')
    candidate = data.get('candidate')
    
    # Step 1: Validate session
    if not session_id or not candidate:
        emit('error', {'message': 'session_id and candidate required'})
        return
    
    # Step 2: Verify sender is in this session (client or compute)
    session_data = active_sessions.get(session_id, {})
    if session_data.get('client_id') != request.sid and \
       session_data.get('compute_id') != request.sid:
        emit('error', {'message': 'Unauthorized: not in this session'})
        return
    
    # Step 3: Relay to other peer in session
    socketio.emit('ice_candidate', {
        'session_id': session_id,
        'candidate': candidate,
        'from': request.sid
    }, room=session_id)
    
    logger.debug(f"ICE candidate relayed for session {session_id}")


if __name__ == '__main__':
    # Step 1: Get port from environment (Render sets PORT automatically)
    port = int(os.environ.get('PORT', 5000))
    
    # Step 2: Run Socket.IO server
    # host='0.0.0.0' binds to all network interfaces
    logger.info(f"Starting NeuraX signaling server on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)


# Notes:
# - Server is stateless: no database, no file storage
# - Sessions tracked in memory only (cleared on restart)
# - All signaling is relaying only: server never inspects SDP/ICE data
# - Room-based messaging enables one-to-one client-compute pairs
# - CORS enabled for cross-origin client deployment
# - Designed for Render.com deployment with Procfile
