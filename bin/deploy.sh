#!/bin/bash
set -eo pipefail

elementIn () {
  # exit 0 if first arg equals any subsequent args
  local e
  for e in "${@:2}"; do [[ "$e" == "$1" ]] && return 0; done
  return 1
}

echo "Logging into the Docker Hub"
docker login -e "$DOCKER_EMAIL" -u "$DOCKER_USERNAME" -p "$DOCKER_PASSWORD" quay.io
echo "Pushing ${DOCKER_IMAGE_TAG} to Docker hub"
docker push ${DOCKER_IMAGE_TAG}
docker tag -f ${DOCKER_IMAGE_TAG} ${DOCKER_REPOSITORY}:last_successful_build
echo "Tagging as last_successful_build"
docker push ${DOCKER_REPOSITORY}:last_successful_build

DEIS_REGIONS=( us-west )
# region where the read-write master database lives
DB_RW_REGION="us-west"

case "$1" in
  "demo")
    DEIS_APP_NAME="basket-demo-${CIRCLE_BRANCH#demo__}"
    # convert underscores to dashes. Deis does _not_ like underscores.
    DEIS_APP_NAME=$( echo "$DEIS_APP_NAME" | tr "_" "-" )
    DEIS_APPS=( $DEIS_APP_NAME )
    DEIS_POST_DEPLOY_APPS=( $DEIS_APP_NAME )
    ;;
  "stage")
    DEIS_APPS=( $DEIS_DEV_APP $DEIS_STAGE_APP $DEIS_ADMIN_STAGE_APP )
    DEIS_POST_DEPLOY_APPS=( $DEIS_DEV_APP $DEIS_STAGE_APP )
    DEIS_REGIONS+=( eu-west )
    ;;
  "prod")
    DEIS_APPS=( $DEIS_PROD_APP $DEIS_ADMIN_APP )
    DEIS_POST_DEPLOY_APPS=( $DEIS_PROD_APP )
    DEIS_REGIONS+=( eu-west )
    ;;
esac

for region in "${DEIS_REGIONS[@]}"; do
  DEIS_CONTROLLER="https://deis.${region}.moz.works"
  echo "Logging into the Deis Controller at $DEIS_CONTROLLER"
  ~/docker/deis login "$DEIS_CONTROLLER" --username "$DEIS_USERNAME" --password "$DEIS_PASSWORD"
  for appname in "${DEIS_APPS[@]}"; do
    # attempt to create the app for demo deploys
    if [[ "$1" == "demo" ]]; then
      echo "Creating the demo app $appname"
      if ~/docker/deis apps:create "$appname" --no-remote; then
        echo "Giving github user $CIRCLE_USERNAME perms for the app"
        ~/docker/deis perms:create "$CIRCLE_USERNAME" -a "$appname" || true
        echo "Configuring the new demo app"
        ~/docker/deis config:push -a "$appname" -p .demo_env
      fi
    fi

    # skip admin apps in DB read-only region
    if [[ "$region" != "$DB_RW_REGION" && "$appname" == *admin* ]]; then
      continue
    fi
    NR_APP="${appname}-${region}"
    echo "Pulling $DOCKER_IMAGE_TAG into Deis app $appname in $region"
    ~/docker/deis pull "$DOCKER_IMAGE_TAG" -a "$appname"

    if [[ "$region" == "$DB_RW_REGION" ]] && elementIn "$appname" "${DEIS_POST_DEPLOY_APPS[@]}"; then
      echo "Running post-deploy tasks for $appname in $region"
      ~/docker/deis run -a "$appname" -- bin/post-deploy.sh
    fi

    if [[ "$1" != "demo" ]]; then
      echo "Pinging New Relic about the deployment of $NR_APP"
      nr_desc="CircleCI built $DOCKER_IMAGE_TAG and deployed it as Deis app $appname in $region"
      curl -H "x-api-key:$NEWRELIC_API_KEY" \
           -d "deployment[app_name]=$NR_APP" \
           -d "deployment[revision]=$CIRCLE_SHA1" \
           -d "deployment[user]=CircleCI" \
           -d "deployment[description]=$nr_desc" \
           https://api.newrelic.com/deployments.xml > /dev/null 2>&1

      echo "Pinging DataDog about the deployment of $NR_APP"
      dd_data=$(cat << EOF
        {
          "title": "Deployment of $NR_APP",
          "text": "$nr_desc",
          "tags": ["region:$region", "environment:$1", "appname:$appname"],
          "aggregation_key": "$NR_APP",
          "source_type_name": "deployment",
          "alert_type": "info"
        }
EOF
      )
      curl -H "Content-type: application/json" -d "$dd_data" \
           "https://app.datadoghq.com/api/v1/events?api_key=$DATADOG_API_KEY" > /dev/null 2>&1
    fi
  done
done
