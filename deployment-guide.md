# Remote MCP Server Deployment Guide

## Overview

The inmydata MCP server can be deployed as a remote web service on AWS, Google Cloud, Azure, or any other hosting platform. The server accepts inmydata credentials securely via HTTP headers in the connection request.

## Deployment Options

### 1. Docker Deployment

**Build the image:**
```bash
docker build -t inmydata-mcp-server .
```

**Run locally:**
```bash
docker run -p 8000:8000 inmydata-mcp-server
```

**Using docker-compose:**
```bash
docker-compose up -d
```

### 2. AWS Deployment

#### Option A: AWS ECS (Elastic Container Service)

1. Push Docker image to ECR:
```bash
aws ecr create-repository --repository-name inmydata-mcp-server
docker tag inmydata-mcp-server:latest <account-id>.dkr.ecr.<region>.amazonaws.com/inmydata-mcp-server:latest
docker push <account-id>.dkr.ecr.<region>.amazonaws.com/inmydata-mcp-server:latest
```

2. Create ECS task definition with the image
3. Create ECS service with load balancer
4. Configure security groups to allow port 8000

#### Option B: AWS App Runner

1. Connect your GitHub repository
2. Configure build settings:
   - Build command: `docker build -t app .`
   - Port: 8000
3. Deploy

#### Option C: AWS Lambda + API Gateway

For serverless deployment, you'll need to adapt the server to use a Lambda handler.

### 3. Google Cloud Platform

#### Cloud Run Deployment

```bash
# Build and deploy in one command
gcloud run deploy inmydata-mcp-server \
  --source . \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000
```

The service URL will be: `https://inmydata-mcp-server-<hash>-uc.a.run.app`

### 4. Azure Container Apps

```bash
az containerapp up \
  --name inmydata-mcp-server \
  --resource-group myResourceGroup \
  --location eastus \
  --ingress external \
  --target-port 8000 \
  --source .
```

### 5. Render.com

1. Create new Web Service
2. Connect your Git repository
3. Configure:
   - Build Command: `pip install uv && uv sync`
   - Start Command: `uv run python server_remote.py sse 8000`
   - Port: 8000
4. Deploy

### 6. Railway.app

1. Create new project from GitHub
2. Configure start command: `python server_remote.py sse 8000`
3. Deploy

### 7. Fly.io

```bash
fly launch
fly deploy
```

## Security Configuration

### Required Headers

Clients must include these headers when connecting:

- `x-inmydata-api-key`: Your inmydata API key
- `x-inmydata-tenant`: Your tenant name
- `x-inmydata-calendar`: Your calendar name
- `x-inmydata-user` (optional): User for chart events (default: mcp-agent)
- `x-inmydata-session-id` (optional): Session ID (default: mcp-session)

### HTTPS in Production

Always use HTTPS in production. Most cloud platforms provide automatic SSL certificates:

- **AWS**: Use Application Load Balancer with ACM certificate
- **GCP Cloud Run**: Automatic HTTPS
- **Azure**: Automatic HTTPS with Container Apps
- **Render/Railway/Fly**: Automatic HTTPS

### CORS Configuration

If you need to add CORS support, modify `server_remote.py` to include CORS middleware.

## Client Configuration

### Claude Desktop / MCP Clients

Create a configuration file (`client-config.json`):

```json
{
  "mcpServers": {
    "inmydata-remote": {
      "url": "https://your-server-url.com/sse",
      "headers": {
        "x-inmydata-api-key": "your-api-key-here",
        "x-inmydata-tenant": "your-tenant-name",
        "x-inmydata-calendar": "your-calendar-name"
      }
    }
  }
}
```

### Python Client Example

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
import asyncio

async def main():
    headers = {
        "x-inmydata-api-key": "your-api-key",
        "x-inmydata-tenant": "your-tenant",
        "x-inmydata-calendar": "your-calendar"
    }
    
    async with sse_client("https://your-server.com/sse", headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # List available tools
            tools = await session.list_tools()
            print(f"Available tools: {[t.name for t in tools.tools]}")
            
            # Call a tool
            result = await session.call_tool("get_financial_year", {})
            print(result)

asyncio.run(main())
```

## Transport Options

The server supports two transport types:

1. **SSE (Server-Sent Events)** - Default, widely compatible
   ```bash
   python server_remote.py sse 8000
   ```
   Endpoint: `http://host:8000/sse`

2. **Streamable HTTP** - New standard, better performance
   ```bash
   python server_remote.py streamable-http 8000
   ```
   Endpoint: `http://host:8000/mcp`

## Monitoring & Logs

### Health Check Endpoint

The FastMCP server provides health check endpoints automatically. Monitor your deployment using:

- SSE transport: `GET /sse` (should return SSE stream)
- HTTP transport: `GET /health` (if configured)

### Application Logs

View logs to monitor requests and errors:

- **Docker**: `docker logs <container-id>`
- **AWS ECS**: CloudWatch Logs
- **GCP Cloud Run**: Cloud Logging
- **Azure**: Log Analytics

## Scaling

For high-traffic deployments:

1. **Horizontal Scaling**: Run multiple instances behind a load balancer
2. **Auto-scaling**: Configure based on CPU/memory metrics
3. **Connection Pooling**: Consider using a reverse proxy (nginx/envoy)

## Troubleshooting

### Connection Refused
- Ensure the server is binding to `0.0.0.0` not `localhost`
- Check firewall/security group rules

### Authentication Errors
- Verify headers are correctly formatted
- Check API key validity in inmydata

### Timeout Issues
- The `get_answer` tool can take up to 60 seconds
- Increase client/load balancer timeout settings

## Environment Variables

Optional environment variables for the remote server:

- `TRANSPORT`: Transport type (sse or streamable-http)
- `PORT`: Server port (default: 8000)

## Cost Optimization

- Use serverless options (Cloud Run, Lambda) for low/variable traffic
- Use containers (ECS, AKS) for consistent high traffic
- Consider cold start times for serverless deployments
