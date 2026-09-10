# License Server

A standalone Python license server for the Douyin Low Latency Viewer.

## Features

- FastAPI-based REST API
- SQLite database with SQLAlchemy ORM
- Secure license key generation and validation
- Device binding (one device per license key)
- Support for license renewal and extension
- UTC timestamps for server time authority

## Business Rules Implemented

- 7-day free trial
- License keys can be redeemed only once
- Device binding for v1 (max_devices = 1)
- State management: UNUSED, ACTIVE, EXPIRED, REVOKED
- Server time is authoritative
- License extension when activating unused keys while current license is active

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Copy environment file:
```bash
cp .env.example .env
```

3. Edit `.env` with your configuration:
```bash
# Generate a secure secret key:
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Set SECRET_KEY and ADMIN_API_KEY in .env
```

## Running the Server

```bash
uvicorn app.main:app --reload
```

The server will be available at:
- API documentation: http://localhost:8000/docs
- ReDoc documentation: http://localhost:8000/redoc

## API Endpoints

### Admin Endpoints

#### Create License
```http
POST /api/v1/admin/licenses
X-API-Key: {your-admin-api-key}
Content-Type: application/json

{
  "duration_days": 30,
  "max_devices": 1
}
```

### License Operations

#### Activate License
```http
POST /api/v1/licenses/activate
Content-Type: application/json

{
  "license_key": "your-license-key-here",
  "device_id": "unique-device-identifier"
}
```

#### Validate License
```http
POST /api/v1/licenses/validate
Content-Type: application/json

{
  "license_key": "your-license-key-here",
  "device_id": "unique-device-identifier"
}
```

## Testing

Run tests with pytest:
```bash
pytest tests/
```

## Database

The application uses SQLite by default. The database file `license_server.db` will be created automatically.

## Security Notes

- Never hardcode admin secrets
- Use environment variables for all sensitive data
- License keys are stored as SHA-256 hashes
- UTC timestamps are used for all time-based operations
- Constant-time comparisons prevent timing attacks

## Development

For development, the server auto-reloads when files change. The admin API key is required for license creation.

## Phase 7.2 Integration

The server provides the following for desktop client integration:
- REST API endpoints for license operations
- JSON response format for easy parsing
- Device binding validation
- Expiration checking with server time authority

The desktop client will need to:
1. Store license keys securely
2. Generate unique device identifiers
3. Make API calls for activation and validation
4. Handle error responses appropriately