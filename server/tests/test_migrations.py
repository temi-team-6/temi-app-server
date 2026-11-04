from .conftest import run_alembic


def test_single_migration_head() -> None:
    result = run_alembic("heads")

    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode == 0, result.stderr
    assert len(heads) == 1, f"마이그레이션 head가 여러 개입니다. `alembic merge heads`로 합치세요: {heads}"


def test_models_match_migrations() -> None:
    result = run_alembic("check")

    assert result.returncode == 0, (
        "models.py 변경에 대응하는 마이그레이션이 없습니다. "
        f"`alembic revision --autogenerate`로 만들어 함께 커밋하세요.\n{result.stdout}{result.stderr}"
    )
