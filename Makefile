# =========================
# CONFIG 
# =========================
PROJECT_ID := techangel
REGION := asia-southeast1
REPO := restaurant-repo
SERVICE := queue-app
IMAGE_NAME := queue-app

IMAGE := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPO)/$(IMAGE_NAME)

# =========================
# COMMANDS
# =========================

# build + push image ไป Artifact Registry
build:
	gcloud builds submit --tag $(IMAGE) .

# deploy ไป Cloud Run
deploy:
	gcloud run deploy $(SERVICE) \
		--image $(IMAGE) \
		--region $(REGION) \
		--platform managed \
		--allow-unauthenticated

# build แล้ว deploy ต่อทันที
build-deploy: build deploy
