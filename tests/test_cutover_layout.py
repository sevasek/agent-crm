"""Prod Traefik overlay, backup mount, and the opt-in deploy workflow."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_traefik_overlay_aliases_crm_and_publishes_no_host_port():
    text = (REPO / "docker-compose.traefik.yml").read_text()
    assert "aliases:" in text
    assert "- crm" in text
    assert "traefik.enable=true" in text
    assert "8000" in text
    assert "127.0.0.1" not in text
    assert "ports: !override []" in text


def test_prod_compose_mounts_backups_and_keeps_memory_cap():
    text = (REPO / "docker-compose.prod.yml").read_text()
    assert text.count("./backups:/app/backups") == 2
    assert "mem_limit: 256m" in text
    assert "APP_UID" in text


def test_entrypoint_prepares_an_existing_backup_dir_and_documents_uid():
    text = (REPO / "docker-entrypoint.sh").read_text()
    assert "BACKUP_DIR:-/app/backups" in text
    assert "1004" in text
    assert 'uid="${APP_UID:-1000}"' in text


def test_deploy_workflow_cannot_fire_for_a_fork():
    text = (REPO / ".github/workflows/deploy.yml").read_text()
    assert "vars.DEPLOY_ENABLED == 'true'" in text
    assert "vars.DEPLOY_REPOSITORY == github.repository" in text
    assert "pull_request" not in text
    assert "docker-compose.traefik.yml" in text
    assert "uid 1004" in text
