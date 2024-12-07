import json
import time
from fastapi import FastAPI, HTTPException, Query
import docker
from docker.types import DeviceRequest
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from fastapi.responses import StreamingResponse
import argparse
import requests

from utils.comfyui_utils import check_comfyui_path, try_install_comfyui
from utils.docker_utils import copy_directories_to_container, create_container, create_mounts, get_container, get_image, pull_image_api, remove_image, restart_container, try_pull_image
from utils.environment_manager import Environment, EnvironmentUpdate, check_environment_name, load_environments, save_environment_to_db, save_environments
from utils.user_settings_manager import UserSettings, load_user_settings, update_user_settings

# Constants
FRONTEND_ORIGIN = "http://localhost:8000"
SIGNAL_TIMEOUT = 2
COMFYUI_PORT = 8188

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Run the FastAPI app with optional ComfyUI path.")
parser.add_argument("--comfyui_path", type=str, help="Default ComfyUI path")
parser.add_argument("--allow_running_multiple_containers", type=str, help="Allow running multiple containers", default="False")
args = parser.parse_args()

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],  # Frontend's origin
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],  # Allow all headers
)

# Routes
@app.post("/environments")
def create_environment(env: Environment):
    """Create a new Docker container and save to local database."""
    environments = load_environments()
    
    try:
        # Check environment name is valid
        check_environment_name(environments, env)
        
        # Check ComfyUI path is valid
        valid_comfyui_path = check_comfyui_path(env.comfyui_path)
        
        # Check if the image is available locally, if not, pull it from Docker Hub
        try_pull_image(env.image)
        
        # Create mounts
        mounts = create_mounts(env.name, env.options.get("mount_config", {}), valid_comfyui_path)
        print(f"Mounts: {mounts}")
        
        # Get port and command
        port = env.options.get("port", COMFYUI_PORT)
        combined_cmd = " --port " + str(port) + " " + env.command
        
        # Get runtime and device requests
        runtime = "nvidia" if env.options.get("runtime", "") == "nvidia" else None
        device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])] if runtime else None
        
        # Create container
        container = create_container(
            image=env.image,
            name=env.name,
            command=combined_cmd,
            # runtime=runtime,
            device_requests=device_requests,
            ports={f"{port}": port},
            mounts=mounts,
        )
        
        env.metadata = {
            "base_image": env.image,
            "created_at": time.time(),
        }

        save_environment_to_db(environments, env, container.id, env.image)
        return {"status": "success", "container_id": container.id}

    except HTTPException:
        # Re-raise HTTPExceptions to ensure they are not caught by the generic exception handler
        raise
    except docker.errors.APIError as e:
        print(f"An API error occurred: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except docker.errors.ImageNotFound:
        print("Image not found. Please check the image name and try again.")
        raise HTTPException(status_code=404, detail="Image not found. Please check the image name and try again.")
    except Exception as e:
        print(f"An error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/environments/{id}/duplicate")
def duplicate_environment(id: str, env: Environment):
    """Duplicate a container by committing its state to an image and running a new container."""
    environments = load_environments()

    try:
        # Check environment name is valid
        check_environment_name(environments, env)
        
        # Check if environment exists
        prev_env = next((e for e in environments if e["id"] == id), None)
        if prev_env is None:
            print(f"Environment not found: {id}")
            raise HTTPException(status_code=404, detail="Environment not found.")
        
        # Check if environment has been activated at least once
        if prev_env.get("status") == "created":
            print(f"Environment can only be duplicated after it has been activated at least once. Please activate the environment first.")
            raise HTTPException(status_code=400, detail="An environment can only be duplicated after it has been activated at least once. Please activate the environment first.")
        
        # Check comfyui path is valid
        check_comfyui_path(prev_env.get("comfyui_path"))
        
        # Create mounts
        mounts = create_mounts(env.name, env.options.get("mount_config", {}), Path(env.comfyui_path))
        print(f"Mounts: {mounts}")
        
        # Get port and command
        port = env.options.get("port", COMFYUI_PORT)
        combined_cmd = " --port " + str(port) + " " + env.command
        
        # Get runtime and device requests
        runtime = "nvidia" if env.options.get("runtime", "") == "nvidia" else None
        device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])] if runtime else None

        # Get existing container and create a unique image
        container = get_container(id)
        image_repo = "comfy-env-clone"
        unique_tag = f"{image_repo}:{env.name}"
        
        try:
            new_image = container.commit(repository=image_repo, tag=env.name)
            print(f"New image created with tag '{unique_tag}': {new_image.id}")
        except docker.errors.APIError as e:
            print(f"An error occurred: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        # Create new container
        new_container = create_container(
            image=unique_tag,
            name=env.name,
            command=combined_cmd,
            # runtime=runtime,
            device_requests=device_requests,
            ports={f"{port}": port},
            mounts=mounts,
        )
        print(f"New container '{env.name}' with id '{new_container.id}' created from the image.")
        
        env.metadata = prev_env.get("metadata", {})
        env.metadata["created_at"] = time.time()

        save_environment_to_db(environments, env, new_container.id, unique_tag, is_duplicate=True)
        return {"status": "success", "container_id": new_container.id}

    except HTTPException:
        # Re-raise HTTPExceptions to ensure they are not caught by the generic exception handler
        raise
    except docker.errors.ImageNotFound:
        print("Image not found. Please check the image name and try again.")
        raise HTTPException(status_code=404, detail="Image not found. Please check the image name and try again.")
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found.")
    except docker.errors.APIError as e:
        print(f"An error occurred: {e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/environments")
def list_environments():
    """List environments from the local database."""
    environments = load_environments()
    return environments


@app.delete("/environments/{id}")
def delete_environment(id: str):
    """Stop and remove a Docker container and update local database."""
    environments = load_environments()

    # Find the environment
    env = next((e for e in environments if e["id"] == id), None)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found.")

    try:
        # Stop and remove the Docker container
        container = get_container(env["id"])
        container.stop(timeout=SIGNAL_TIMEOUT)
        container.remove()

        # If the environment is a duplicate, try to remove its backing image
        if env.get("duplicate", False):
            try:
                remove_image(env["image"], force=True)
                print(f"Backing image '{env['image']}' removed.")
            except docker.errors.ImageNotFound:
                print(f"Backing image '{env['image']}' not found.")
            except docker.errors.APIError as e:
                print(f"Error removing image '{env['image']}': {e}")
                raise HTTPException(status_code=400, detail=f"Error removing image: {str(e)}")

        # Update the database
        environments = [e for e in environments if e["id"] != id]
        save_environments(environments)
        return {"status": "success", "id": id}
    except docker.errors.NotFound:
        # If container is not found, just update the database
        environments = [e for e in environments if e["id"] != id]
        save_environments(environments)
        return {"status": "success (container not found)", "id": id}
    except docker.errors.APIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/environments/{name}/status")
def get_environment_status(name: str):
    """Get the status of a Docker container."""
    environments = load_environments()
    env = next((e for e in environments if e["name"] == name), None)
    return {"status": env["status"]}

@app.put("/environments/{id}")
def update_environment(id: str, env: EnvironmentUpdate):
    """Update an environment in the local database."""
    environments = load_environments()
    
    # Get existing environment
    existing_env = next((e for e in environments if e["id"] == id), None)
    if existing_env is None:
        raise HTTPException(status_code=404, detail="Environment not found.")
    
    # Update the environment name
    if env.name is not None:
        if any(e["name"] == env.name for e in environments):
            raise HTTPException(status_code=400, detail="Environment name already exists.")
        # Try renaming the container:
        try:
            container = get_container(existing_env["id"])
            container.rename(env.name)
        except docker.errors.NotFound:
            raise HTTPException(status_code=404, detail="Container not found.")
        except docker.errors.APIError as e:
            raise HTTPException(status_code=400, detail=str(e))
        existing_env["name"] = env.name
    
    save_environments(environments)
    return {"status": "success", "container_id": id}


@app.post("/environments/{id}/activate")
def activate_environment(id: str, options: dict = {}):
    print(options)
    """Activate a Docker container."""
    environments = load_environments()
    
    try:
        # Get environment
        env = next((e for e in environments if e["id"] == id), None)
        if env is None:
            raise HTTPException(status_code=404, detail="Environment not found.")
        
        # Load env into Environment object
        env = Environment(**env)
        
        # Get container
        container = get_container(env.id)
        
        # Stop all other running containers if they exist:
        print(args.allow_running_multiple_containers)
        if args.allow_running_multiple_containers != "True":
            for e in environments:
                if e["id"] != id and e["status"] == "running":
                    try:
                        temp_container = get_container(e["id"])
                        temp_container.stop(timeout=SIGNAL_TIMEOUT)
                    except docker.errors.NotFound:
                        pass
                    except docker.errors.APIError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                
        # Start container if it is not running
        if not container.status == "running":
            container.start()
        
        # Get comfyui path
        comfyui_path = Path(env.comfyui_path)
        
        # Check mount_config for directories to copy
        mount_config = env.options.get("mount_config", "{}")

        if env.status == "created":
            installed_custom_nodes = copy_directories_to_container(id, comfyui_path, mount_config)
            if installed_custom_nodes:
                restart_container(id)

        env.status = "running"
        save_environments(environments)
        return {"status": "success", "container_id": id}
    except HTTPException:
        # Re-raise HTTPExceptions to ensure they are not caught by the generic exception handler
        raise
    except docker.errors.APIError as e:
        print(f"An API error occurred: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found.")
    except Exception as e:
        print(f"An error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/environments/{id}/deactivate")
def deactivate_environment(id: str):
    """Deactivate a Docker container."""
    environments = load_environments()
    try:
        # Get environment
        env = next((e for e in environments if e["id"] == id), None)
        if env is None:
            raise HTTPException(status_code=404, detail="Environment not found.")
        
        # Get container
        container = get_container(env["id"])

        # Return success if container is not running
        if container.status == "stopped" or container.status == "exited" or container.status == "created" or container.status == "dead":
            return {"status": "success", "container_id": id}

        # Stop container
        container.stop(timeout=SIGNAL_TIMEOUT)

        # Update environment status
        env["status"] = "stopped"
        save_environments(environments)
        return {"status": "success", "container_id": id}
    except HTTPException:
        raise
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found.")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"An error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/user-settings")
def get_user_settings():
    """Get user settings."""
    default_comfyui_path = args.comfyui_path if args.comfyui_path else ""
    return load_user_settings(default_comfyui_path)

@app.put("/user-settings")
def update_user(settings: UserSettings):
    """Update user settings."""
    print(settings)
    update_user_settings(settings.model_dump())
    return {"status": "success"}

@app.get("/images/tags")
def get_image_tags():
    """Get all available image tags from Docker Hub."""
    try:
        response = requests.get(
            "https://hub.docker.com/v2/namespaces/akatzai/repositories/comfyui-env/tags?page_size=100"
        )
        response.raise_for_status()  # Raise an error for bad responses
        data = response.json()
        tags = [tag['name'] for tag in data.get('results', [])]
        return {"tags": tags}
    except requests.exceptions.RequestException as e:
        print(f"Error fetching tags from Docker Hub: {e}")
        raise HTTPException(status_code=500, detail="Error fetching tags from Docker Hub")

@app.get("/images/exists")
def check_image(image: str = Query(..., description="The name of the Docker image to check")):
    print(image)
    try:
        get_image(image)
        return {"status": "found"}
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail="Image not found locally. Ready to pull.")
    
@app.get("/images/pull")
def pull_image(image: str = Query(..., description="The name of the Docker image to pull")):
    def image_pull_stream():
        layers = {}
        total_download_size = 0
        total_downloaded = 0
        completed_layers = set()
        already_exist_layers = set()

        try:
            # Start pulling the image
            for line in pull_image_api(image):
                # Send raw line for debugging (optional)
                # yield f"data: {json.dumps(line)}\n\n"

                status = line.get('status')
                layer_id = line.get('id')
                progress_detail = line.get('progressDetail', {})

                if layer_id:
                    if status == "Pull complete":
                        completed_layers.add(layer_id)
                    elif status == "Already exists":
                        already_exist_layers.add(layer_id)
                    elif 'current' in progress_detail and 'total' in progress_detail:
                        current = progress_detail.get('current', 0)
                        total = progress_detail.get('total', 0)

                        if total > 0:
                            if layer_id not in layers:
                                layers[layer_id] = {'current': current, 'total': total}
                                total_download_size += total
                                total_downloaded += current
                            else:
                                total_downloaded -= layers[layer_id]['current']
                                layers[layer_id]['current'] = current
                                total_downloaded += current

                        # Compute overall progress
                        if total_download_size > 0:
                            overall_progress = (total_downloaded / total_download_size) * 100
                        else:
                            overall_progress = 0

                        # Send progress update
                        yield f"data: {json.dumps({'progress': overall_progress})}\n\n"

            # When done, send completion status
            yield f"data: {json.dumps({'progress': 100, 'status': 'completed'})}\n\n"

        except docker.errors.APIError as e:
            error_message = f"Error pulling image {image}: {e}"
            yield f"data: {json.dumps({'error': error_message})}\n\n"

    return StreamingResponse(image_pull_stream(), media_type="text/event-stream")

@app.post("/valid-comfyui-path")
def get_valid_comfyui_path(obj: dict):
    """Get the valid ComfyUI path."""
    valid_comfyui_path = check_comfyui_path(obj["path"])
    return {"valid_comfyui_path": str(valid_comfyui_path)}

@app.post("/install-comfyui")
def install_comfyui(obj: dict):
    """Install ComfyUI at given path."""
    print(obj)
    try_install_comfyui(obj["path"], obj["branch"])
    return {"status": "success"}

@app.get("/environments/{id}/logs")
def stream_container_logs(id: str):
    """Stream logs from a running Docker container."""
    try:
        container = get_container(id)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found.")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if container.status != "running":
        raise HTTPException(status_code=400, detail="Container is not running.")

    def log_generator():
        for log in container.logs(stream=True):
            decoded_log = log.decode('utf-8')
            yield f"data: {decoded_log}\n\n"

    return StreamingResponse(log_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5172)
