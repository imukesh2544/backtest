// ─── StratEdge Jenkins CI/CD Pipeline ───────────────────────
// Triggered on every push to main branch.
// Pipeline:
//   1. Checkout
//   2. Build Docker image
//   3. Push to local registry (or Docker Hub — configurable)
//   4. Deploy to Kubernetes via kubectl
//   5. Verify rollout
//
// PREREQUISITES on Jenkins agent:
//   - Docker installed and jenkins user in docker group
//   - kubectl installed and configured (~/.kube/config pointing to your cluster)
//   - Git plugin installed in Jenkins
//
// CONFIGURE THESE before first run:
//   REGISTRY      — your Docker registry (localhost:5000 for local, or docker.io/youruser)
//   KUBE_CONFIG   — Jenkins credential ID for kubeconfig file (if not on same machine)

pipeline {
    agent any

    // ── Environment ───────────────────────────────────────────
    environment {
        APP_NAME    = 'stratedge'
        NAMESPACE   = 'stratedge'
        REGISTRY    = 'localhost:5000'                     // Change to your registry
        IMAGE_NAME  = "${REGISTRY}/${APP_NAME}"
        IMAGE_TAG   = "${BUILD_NUMBER}"
        IMAGE_FULL  = "${IMAGE_NAME}:${IMAGE_TAG}"
        IMAGE_LATEST= "${IMAGE_NAME}:latest"
    }

    options {
        buildDiscarder(logRotator(numToKeepStr: '10'))
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    // ── Triggers ──────────────────────────────────────────────
    triggers {
        // Poll SCM every 2 minutes (or configure GitHub webhook instead)
        pollSCM('H/2 * * * *')
    }

    stages {

        // ── Stage 1: Checkout ──────────────────────────────────
        stage('Checkout') {
            steps {
                echo "==> Checking out source code"
                checkout scm
                script {
                    env.GIT_COMMIT_SHORT = sh(
                        script: "git rev-parse --short HEAD",
                        returnStdout: true
                    ).trim()
                    echo "Commit: ${env.GIT_COMMIT_SHORT}  Build: ${BUILD_NUMBER}"
                }
            }
        }

        // ── Stage 2: Lint / Sanity check ──────────────────────
        stage('Lint') {
            steps {
                echo "==> Running syntax checks"
                sh '''
                    python3 -c "import ast, sys; ast.parse(open('app.py').read()); print('app.py syntax OK')"
                    python3 -c "import json, sys; json.load(open('stocks.json')); print('stocks.json valid')"
                '''
            }
        }

        // ── Stage 3: Build Docker image ────────────────────────
        stage('Build Image') {
            steps {
                echo "==> Building Docker image: ${IMAGE_FULL}"
                sh """
                    docker build \
                        --tag ${IMAGE_FULL} \
                        --tag ${IMAGE_LATEST} \
                        --label git-commit=${env.GIT_COMMIT_SHORT} \
                        --label build-number=${BUILD_NUMBER} \
                        --label build-date=\$(date -u +%Y-%m-%dT%H:%M:%SZ) \
                        .
                """
            }
        }

        // ── Stage 4: Push to registry ─────────────────────────
        stage('Push Image') {
            steps {
                echo "==> Pushing image to registry"
                sh """
                    docker push ${IMAGE_FULL}
                    docker push ${IMAGE_LATEST}
                """
                // Clean up local dangling images after push
                sh "docker image prune -f || true"
            }
        }

        // ── Stage 5: Apply K8s manifests ──────────────────────
        stage('Apply Manifests') {
            steps {
                echo "==> Applying Kubernetes manifests"
                sh """
                    kubectl apply -f k8s/namespace.yaml
                    kubectl apply -f k8s/pvc.yaml
                    kubectl apply -f k8s/deployment.yaml
                    kubectl apply -f k8s/service.yaml
                    kubectl apply -f k8s/ingress.yaml
                """
            }
        }

        // ── Stage 6: Update image in deployment ───────────────
        stage('Deploy') {
            steps {
                echo "==> Rolling out new image: ${IMAGE_FULL}"
                sh """
                    kubectl set image deployment/${APP_NAME} \
                        ${APP_NAME}=${IMAGE_FULL} \
                        -n ${NAMESPACE}

                    # Annotate with build info for auditing
                    kubectl annotate deployment/${APP_NAME} \
                        -n ${NAMESPACE} \
                        kubernetes.io/change-cause="Build #${BUILD_NUMBER} commit ${env.GIT_COMMIT_SHORT}" \
                        --overwrite
                """
            }
        }

        // ── Stage 7: Verify rollout ────────────────────────────
        stage('Verify') {
            steps {
                echo "==> Waiting for rollout to complete"
                sh """
                    kubectl rollout status deployment/${APP_NAME} \
                        -n ${NAMESPACE} \
                        --timeout=180s
                """
                echo "==> Checking pod health"
                sh """
                    kubectl get pods -n ${NAMESPACE} -l app=${APP_NAME}
                    sleep 10
                    # Verify at least one pod is running and ready
                    READY=\$(kubectl get deployment ${APP_NAME} -n ${NAMESPACE} -o jsonpath='{.status.readyReplicas}')
                    if [ "\$READY" -lt "1" ]; then
                        echo "ERROR: No ready replicas after deploy!"
                        kubectl describe deployment ${APP_NAME} -n ${NAMESPACE}
                        kubectl logs -l app=${APP_NAME} -n ${NAMESPACE} --tail=50
                        exit 1
                    fi
                    echo "==> Deploy successful — \$READY replica(s) ready"
                """
            }
        }
    }

    // ── Post Actions ──────────────────────────────────────────
    post {
        success {
            echo """
╔══════════════════════════════════════╗
║  ✓ StratEdge deployed successfully  ║
║  Build: #${BUILD_NUMBER}             ║
║  Commit: ${env.GIT_COMMIT_SHORT}     ║
╚══════════════════════════════════════╝
            """
            // Get the node IP for convenience
            sh """
                NODE_IP=\$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
                INGRESS_PORT=\$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.ports[?(@.port==80)].nodePort}' 2>/dev/null || echo "80")
                echo "==> Access StratEdge at: http://\$NODE_IP:\$INGRESS_PORT"
            """
        }
        failure {
            echo "==> DEPLOY FAILED — collecting debug info"
            sh """
                kubectl get events -n ${NAMESPACE} --sort-by='.lastTimestamp' | tail -20 || true
                kubectl logs -l app=${APP_NAME} -n ${NAMESPACE} --tail=100 || true
            """
            // Rollback on failure
            sh """
                echo "==> Rolling back to previous version"
                kubectl rollout undo deployment/${APP_NAME} -n ${NAMESPACE} || true
            """
        }
        always {
            // Clean workspace
            cleanWs()
        }
    }
}
