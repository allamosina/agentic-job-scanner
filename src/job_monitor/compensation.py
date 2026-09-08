from datetime import date
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, model_validator

from .config import Strict

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class Salary(Strict):
    lower: float = Field(ge=0)
    upper: float = Field(ge=0)
    currency: str
    period: Literal["year", "month", "hour", "day", "unknown"]
    is_gross_base: bool
    quote: str

    @model_validator(mode="after")
    def ordered(self):
        if self.upper < self.lower:
            raise ValueError("Salary range is reversed")
        return self


async def exchange_rates(web):
    response = await web.get(ECB_URL)
    root = ElementTree.fromstring(response.content)
    rates, as_of = {"EUR": 1.0}, None
    for element in root.iter():
        if "time" in element.attrib:
            as_of = element.attrib["time"]
        if "currency" in element.attrib:
            rate = float(element.attrib["rate"])
            if rate > 0:
                rates[element.attrib["currency"]] = rate
    if not as_of or "CZK" not in rates:
        raise ValueError("ECB rates unavailable")
    # Fail closed for stale FX; weekend/holiday rates remain usable with their date shown.
    if not 0 <= (date.today() - date.fromisoformat(as_of)).days <= 7:
        raise ValueError("ECB rates stale or future-dated")
    return {"date": as_of, "rates_per_eur": rates, "source": ECB_URL}


def normalize_salary(salary: Salary | None, employment_model, fx, bands):
    if salary is None:
        return {"fit": "UNKNOWN", "summary": "Нет подтверждённого диапазона зарплаты"}
    raw = f"{salary.lower:g}–{salary.upper:g} {salary.currency} / {salary.period}"
    if not salary.is_gross_base or salary.period not in {"month", "year"}:
        return {"fit": "UNKNOWN", "summary": raw + "; не подтверждена месячная/годовая gross base"}
    if employment_model == "contractor":
        return {"fit": "UNKNOWN", "summary": raw + "; contractor, напрямую с employee base не сравнивается"}
    if employment_model == "unknown":
        return {"fit": "UNKNOWN", "summary": raw + "; модель оформления неизвестна"}
    annual = (
        [salary.lower, salary.upper] if salary.period == "year" else [salary.lower * 12, salary.upper * 12]
    )
    currency = salary.currency.upper()
    result = {"annual_original": annual, "currency": currency}
    if currency == "CZK":
        monthly = [x / 12 for x in annual]
    elif fx and currency in fx["rates_per_eur"]:
        eur = [x / fx["rates_per_eur"][currency] for x in annual]
        monthly = [x * fx["rates_per_eur"]["CZK"] / 12 for x in eur]
        result.update(annual_eur=eur, fx_date=fx["date"], fx_source=fx["source"])
    else:
        return {**result, "fit": "UNKNOWN", "summary": raw + "; курс для сравнения недоступен"}

    def band(value):
        return "HIGH" if value >= bands["high_min"] else "MEDIUM" if value >= bands["medium_min"] else "LOW"

    categories = [band(x) for x in monthly]
    fit = categories[0] if categories[0] == categories[1] else "UNKNOWN"
    summary = raw + f"; ≈ {monthly[0]:,.0f}–{monthly[1]:,.0f} CZK gross base/месяц"
    if "annual_eur" in result:
        summary += (
            f"; ≈ {result['annual_eur'][0]:,.0f}–{result['annual_eur'][1]:,.0f} EUR/год; курс {fx['date']}"
        )
    if categories[0] != categories[1]:
        summary += "; диапазон пересекает " + "–".join(categories)
    if monthly[0] < bands["recent_base"]:
        summary += f"; нижняя граница ниже ориентира {bands['recent_base']:,} CZK"
    return {**result, "monthly_czk": monthly, "fit": fit, "summary": summary}
