// A small Jenkins pipeline that runs the same Docker commands as .github/workflows/tests.yml.
// GitHub Actions is the project's main CI; this is the same core flow as a Jenkinsfile.
//
// Mirrored here: build, API + UI + database tests, performance tests, the QA KPI review.
// Not mirrored: the Kubernetes jobs (chaos and rolling upgrades), the Allure report on GitHub Pages and
// the AWS QA environment. They need kind, a public site and cloud credentials; GitHub Actions runs them.
//
// Needs a Jenkins agent with Docker, Docker Compose and python3 (the KPI review uses only the standard library).
// Try it locally: jenkins/jenkins:lts-jdk17 with the Docker socket mounted, then a Multibranch Pipeline job
// ("Pipeline script from SCM") pointing at this repository.
pipeline {
    agent any

    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    environment {
        // 3x load on main and 1x elsewhere, as in GitHub Actions (needs a multibranch job for BRANCH_NAME)
        PERF_LOAD = "${env.BRANCH_NAME == 'main' ? '3' : '1'}"
    }

    stages {
        stage('Build') {
            steps {
                sh 'docker compose build'
            }
        }

        stage('API + UI + database tests') {
            steps {
                sh 'docker compose run --rm tests'
            }
            post {
                always {
                    // the performance stage overwrites junit.xml and api.log, so keep this run's files apart
                    sh 'mkdir -p reports/functional && cp reports/junit.xml reports/report.html reports/api*.log reports/functional/ || true'
                }
            }
        }

        stage('Performance tests') {
            steps {
                sh 'docker compose run --rm tests pytest -m perf'
            }
            post {
                always {
                    sh 'mkdir -p reports/performance && cp reports/junit.xml reports/api.log reports/performance.* reports/performance/ || true'
                }
            }
        }
    }

    post {
        always {
            // The KPI review reports; it never fails the build (the tests do that).
            sh 'python3 -m utils.kpi --functional reports/functional --perf reports/performance --out reports/kpi > reports/kpi-summary.md || true'
            junit allowEmptyResults: true, testResults: 'reports/functional/junit.xml, reports/performance/junit.xml'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
            // the API's log only when something failed, and before `down` removes the containers
            script {
                if (currentBuild.currentResult != 'SUCCESS') {
                    sh 'docker compose logs api || true'
                }
            }
            sh 'docker compose down -v || true'
        }
    }
}
