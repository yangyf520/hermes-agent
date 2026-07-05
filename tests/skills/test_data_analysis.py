"""Tests for optional-skills/data-science/data-analysis/scripts/analyze.py."""

import csv
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "data-science"
    / "data-analysis"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import analyze


class TestSanitizeTableName:
    def test_spaces_and_digits(self):
        assert analyze.sanitize_table_name("2024 Sales") == "t_2024_Sales"


class TestComputeFilesHash:
    def test_order_independent(self, tmp_path):
        one = tmp_path / "one.csv"
        two = tmp_path / "two.csv"
        one.write_text("a\n1\n", encoding="utf-8")
        two.write_text("b\n2\n", encoding="utf-8")
        assert analyze.compute_files_hash([str(one), str(two)]) == analyze.compute_files_hash(
            [str(two), str(one)]
        )


class TestCsvPipeline:
    @pytest.fixture
    def sample_csv(self, tmp_path):
        path = tmp_path / "sales.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["category", "amount"])
            writer.writerow(["A", "10"])
            writer.writerow(["B", "20"])
        return path

    def test_load_and_query_csv(self, sample_csv):
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect(":memory:")
        table_map: dict[str, str] = {}
        analyze._load_csv(con, str(sample_csv), table_map)
        table = table_map["sales"]
        rows = con.execute(
            f'SELECT category, SUM(CAST(amount AS INTEGER)) FROM "{table}" GROUP BY category'
        ).fetchall()
        con.close()
        assert len(rows) == 2


class TestOptionalSkillFrontmatter:
    @pytest.mark.parametrize(
        "skill_dir",
        ["data-analysis", "chart-visualization", "consulting-analysis"],
    )
    def test_description_length(self, skill_dir):
        skill_md = (
            Path(__file__).resolve().parents[2]
            / "optional-skills"
            / "data-science"
            / skill_dir
            / "SKILL.md"
        )
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"')
                assert len(desc) <= 60, (skill_dir, len(desc), desc)
                return
        pytest.fail(f"no description in {skill_dir}")
