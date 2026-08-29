DEST := "tobi@192.168.178.56"
DEST_DIR := "~/meshcore-bot/"
SUPERVISOR_NAME := "meshcore-bot"

deploy:
    rsync -vhra ./ {{ DEST }}:{{ DEST_DIR }} \
    --include='**.gitignore' \
    --exclude='/.git' \
    --filter=':- .gitignore' \
    --delete-after
    ssh -t {{ DEST }} sudo supervisorctl restart {{ SUPERVISOR_NAME }}
