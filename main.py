from integration_layer.app import create_app

app = create_app()


@app.get("/")
def read_root():
    return {"service": "tenant-api-integration-layer", "versions": ["v1", "v2"]}
