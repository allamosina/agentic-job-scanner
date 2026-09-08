import pytest

from job_monitor.compensation import Salary, normalize_salary


def salary(**changes):
    return Salary.model_validate(
        {
            "lower": 100000,
            "upper": 100000,
            "currency": "CZK",
            "period": "month",
            "is_gross_base": True,
            "quote": "100000 CZK gross",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "amount,fit",
    [(100000, "HIGH"), (90000, "HIGH"), (89999, "MEDIUM"), (60000, "MEDIUM"), (59999, "LOW")],
)
def test_czk_bands(amount, fit, preferences):
    assert (
        normalize_salary(salary(lower=amount, upper=amount), "employee", None, preferences.compensation_czk)[
            "fit"
        ]
        == fit
    )


def test_foreign_base_preserves_original_and_fx_date(preferences):
    fx = {"date": "2026-09-07", "source": "ecb-test", "rates_per_eur": {"EUR": 1, "USD": 1.2, "CZK": 24}}
    result = normalize_salary(
        salary(lower=54000, upper=54000, currency="USD", period="year"),
        "employee",
        fx,
        preferences.compensation_czk,
    )
    assert result["annual_original"] == [54000, 54000]
    assert result["annual_eur"] == [45000, 45000]
    assert result["monthly_czk"] == [90000, 90000]
    assert result["fx_date"] == "2026-09-07"


def test_contractor_not_equated_to_employee(preferences):
    result = normalize_salary(salary(), "contractor", None, preferences.compensation_czk)
    assert result["fit"] == "UNKNOWN"
    assert "monthly_czk" not in result


def test_range_crossing_band_not_optimistically_high(preferences):
    result = normalize_salary(
        salary(lower=50000, upper=110000), "employee", None, preferences.compensation_czk
    )
    assert result["fit"] == "UNKNOWN"
    assert "LOW–HIGH" in result["summary"]


def test_total_comp_not_treated_as_base(preferences):
    assert (
        normalize_salary(salary(is_gross_base=False), "employee", None, preferences.compensation_czk)["fit"]
        == "UNKNOWN"
    )


def test_missing_fx_does_not_invent_rate(preferences):
    assert (
        normalize_salary(salary(currency="USD"), "employee", None, preferences.compensation_czk)["fit"]
        == "UNKNOWN"
    )
