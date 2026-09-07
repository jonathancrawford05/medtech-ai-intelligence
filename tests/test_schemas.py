"""Schema tests: Pydantic models and their Spark StructType mirrors must agree."""

from __future__ import annotations

import datetime as dt

import pytest
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    DoubleType,
    LongType,
    StringType,
    StructType,
    TimestampType,
)

from registry import schemas
from registry.schemas import (
    CompanyRecord,
    DeviceRecord,
    EvidenceRecord,
    spark_schema_for,
)


class TestTypeMapping:
    def test_maps_primitives(self):
        fields = {f.name: f.dataType for f in spark_schema_for(DeviceRecord).fields}
        assert isinstance(fields["submission_number"], StringType)
        assert isinstance(fields["decision_date"], DateType)
        assert isinstance(fields["has_pccp"], BooleanType)
        assert isinstance(fields["predicate_age_days"], LongType)

    def test_maps_optional_float(self):
        fields = {f.name: f.dataType for f in spark_schema_for(EvidenceRecord).fields}
        assert isinstance(fields["sensitivity"], DoubleType)

    def test_maps_list_of_str_to_array(self):
        fields = {f.name: f.dataType for f in spark_schema_for(CompanyRecord).fields}
        assert isinstance(fields["raw_name_variants"], ArrayType)
        assert isinstance(fields["raw_name_variants"].elementType, StringType)

    def test_literal_maps_to_string(self):
        fields = {f.name: f.dataType for f in spark_schema_for(DeviceRecord).fields}
        assert isinstance(fields["pathway"], StringType)
        assert isinstance(fields["device_class"], StringType)

    def test_nullability_follows_optionality(self):
        fields = {f.name: f for f in spark_schema_for(DeviceRecord).fields}
        assert fields["submission_number"].nullable is False
        assert fields["applicant_resolved"].nullable is True

    def test_datetime_maps_to_timestamp(self):
        fields = {
            f.name: f.dataType for f in spark_schema_for(schemas.BronzeFdaAiListRecord).fields
        }
        assert isinstance(fields["ingested_at"], TimestampType)

    def test_unsupported_type_raises(self):
        from pydantic import BaseModel

        class Bad(BaseModel):
            thing: complex

        with pytest.raises(TypeError, match="Unsupported"):
            spark_schema_for(Bad)


class TestFieldParity:
    """A generated schema is only useful if it cannot drift from the model."""

    @pytest.mark.parametrize(
        "model", [DeviceRecord, EvidenceRecord, CompanyRecord, schemas.BronzeFdaAiListRecord]
    )
    def test_every_model_field_appears_in_the_struct(self, model):
        struct = spark_schema_for(model)
        assert [f.name for f in struct.fields] == list(model.model_fields)

    def test_schema_is_a_struct_type(self):
        assert isinstance(spark_schema_for(DeviceRecord), StructType)


class TestDeviceRecord:
    def _valid(self, **overrides):
        base = {
            "submission_number": "K243456",
            "device_name": "CaRi-Heart",
            "applicant_raw": "Caristo Diagnostics Ltd",
            "decision_date": dt.date(2024, 11, 1),
            "pathway": "510k",
            "specialty_panel": "Radiology",
            "specialty_category": "cardiovascular",
            "product_code": "QIH",
            "device_class": "II",
            "has_pccp": False,
            "cybersecurity_statement_present": True,
            "source_url": "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K243456",
        }
        base.update(overrides)
        return DeviceRecord(**base)

    def test_valid_record_round_trips(self):
        rec = self._valid()
        assert rec.submission_number == "K243456"
        assert rec.applicant_resolved is None

    def test_submission_number_is_normalised_to_upper(self):
        assert self._valid(submission_number=" k243456 ").submission_number == "K243456"

    def test_rejects_unknown_pathway(self):
        with pytest.raises(ValueError):
            self._valid(pathway="premarket_notification")

    def test_rejects_unknown_device_class(self):
        with pytest.raises(ValueError):
            self._valid(device_class="IV")

    def test_rejects_blank_submission_number(self):
        with pytest.raises(ValueError):
            self._valid(submission_number="   ")


class TestEvidenceRecord:
    def _valid(self, **overrides):
        base = {
            "submission_number": "K243456",
            "reports_sensitivity_specificity": True,
            "sensitivity": 0.91,
            "specificity": 0.88,
            "discloses_demographics": False,
            "intended_use_text": "Estimates cardiovascular risk of death.",
            "mortality_keyword_flag": True,
        }
        base.update(overrides)
        return EvidenceRecord(**base)

    def test_valid_record(self):
        rec = self._valid()
        assert rec.mortality_keyword_flag is True

    def test_confirmed_flag_defaults_to_unreviewed(self):
        """None means 'no human/LLM has confirmed yet' -- distinct from False."""
        assert self._valid().mortality_confirmed_flag is None

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_sensitivity_must_be_a_proportion(self, bad):
        with pytest.raises(ValueError):
            self._valid(sensitivity=bad)

    def test_confirmation_requires_a_review_method(self):
        """An audit trail is the point: a confirmed flag with no provenance is invalid."""
        with pytest.raises(ValueError, match="review_method"):
            self._valid(mortality_confirmed_flag=True)

    def test_confirmation_with_review_method_is_valid(self):
        rec = self._valid(mortality_confirmed_flag=True, mortality_review_method="human")
        assert rec.mortality_confirmed_flag is True


class TestCompanyRecord:
    def test_valid_record(self):
        rec = CompanyRecord(
            resolved_name="Abbott Laboratories",
            raw_name_variants=["Abbott", "Abbott Labs"],
            cumulative_device_count=12,
        )
        assert rec.acquisition_history == []
        assert rec.current_parent is None

    def test_device_count_cannot_be_negative(self):
        with pytest.raises(ValueError):
            CompanyRecord(resolved_name="X", cumulative_device_count=-1)


@pytest.mark.spark
class TestSparkInterop:
    """The generated schemas must actually work as Spark schemas."""

    def test_device_rows_land_in_a_typed_delta_table(self, spark, lakehouse):
        from registry import tables

        rec = DeviceRecord(
            submission_number="K243456",
            device_name="CaRi-Heart",
            applicant_raw="Caristo Diagnostics Ltd",
            decision_date=dt.date(2024, 11, 1),
            pathway="510k",
            specialty_panel="Radiology",
            specialty_category="cardiovascular",
            product_code="QIH",
            device_class="II",
            has_pccp=False,
            cybersecurity_statement_present=True,
            source_url="https://example.org/K243456",
        )
        df = spark.createDataFrame([rec.model_dump()], schema=spark_schema_for(DeviceRecord))
        tables.write_table(df, lakehouse, "silver_devices", mode="overwrite")

        back = tables.read_table(spark, lakehouse, "silver_devices")
        assert back.count() == 1
        row = back.first()
        assert row["submission_number"] == "K243456"
        assert row["decision_date"] == dt.date(2024, 11, 1)
