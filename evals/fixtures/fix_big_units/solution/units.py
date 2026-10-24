"""Unit conversions. Every function takes a number and returns the converted number."""

import math


def _check(value):
    """Reject values that are not finite numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return value


def km_to_miles(value):
    """Convert km to miles.

    Args:
        value: an amount in km, as an int or float.

    Returns:
        The same amount in miles, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: km_to_miles(2) == 2 * 0.621371
    """
    return _check(value) * 0.621371


def miles_to_km(value):
    """Convert miles to km.

    Args:
        value: an amount in miles, as an int or float.

    Returns:
        The same amount in km, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: miles_to_km(2) == 2 * 1.609344
    """
    return _check(value) * 1.609344


def m_to_feet(value):
    """Convert m to feet.

    Args:
        value: an amount in m, as an int or float.

    Returns:
        The same amount in feet, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: m_to_feet(2) == 2 * 3.28084
    """
    return _check(value) * 3.28084


def feet_to_m(value):
    """Convert feet to m.

    Args:
        value: an amount in feet, as an int or float.

    Returns:
        The same amount in m, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: feet_to_m(2) == 2 * 0.3048
    """
    return _check(value) * 0.3048


def cm_to_inches(value):
    """Convert cm to inches.

    Args:
        value: an amount in cm, as an int or float.

    Returns:
        The same amount in inches, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: cm_to_inches(2) == 2 * 0.393701
    """
    return _check(value) * 0.393701


def inches_to_cm(value):
    """Convert inches to cm.

    Args:
        value: an amount in inches, as an int or float.

    Returns:
        The same amount in cm, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: inches_to_cm(2) == 2 * 2.54
    """
    return _check(value) * 2.54


def kg_to_pounds(value):
    """Convert kg to pounds.

    Args:
        value: an amount in kg, as an int or float.

    Returns:
        The same amount in pounds, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kg_to_pounds(2) == 2 * 2.20462
    """
    return _check(value) * 2.20462


def pounds_to_kg(value):
    """Convert pounds to kg.

    Args:
        value: an amount in pounds, as an int or float.

    Returns:
        The same amount in kg, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: pounds_to_kg(2) == 2 * 0.453592
    """
    return _check(value) * 0.453592


def g_to_ounces(value):
    """Convert g to ounces.

    Args:
        value: an amount in g, as an int or float.

    Returns:
        The same amount in ounces, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: g_to_ounces(2) == 2 * 0.035274
    """
    return _check(value) * 0.035274


def ounces_to_g(value):
    """Convert ounces to g.

    Args:
        value: an amount in ounces, as an int or float.

    Returns:
        The same amount in g, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: ounces_to_g(2) == 2 * 28.3495
    """
    return _check(value) * 28.3495


def liters_to_gallons(value):
    """Convert liters to gallons.

    Args:
        value: an amount in liters, as an int or float.

    Returns:
        The same amount in gallons, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: liters_to_gallons(2) == 2 * 0.264172
    """
    return _check(value) * 0.264172


def gallons_to_liters(value):
    """Convert gallons to liters.

    Args:
        value: an amount in gallons, as an int or float.

    Returns:
        The same amount in liters, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: gallons_to_liters(2) == 2 * 3.78541
    """
    return _check(value) * 3.78541


def ml_to_cups(value):
    """Convert ml to cups.

    Args:
        value: an amount in ml, as an int or float.

    Returns:
        The same amount in cups, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: ml_to_cups(2) == 2 * 0.00422675
    """
    return _check(value) * 0.00422675


def cups_to_ml(value):
    """Convert cups to ml.

    Args:
        value: an amount in cups, as an int or float.

    Returns:
        The same amount in ml, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: cups_to_ml(2) == 2 * 236.588
    """
    return _check(value) * 236.588


def kmh_to_mph(value):
    """Convert kmh to mph.

    Args:
        value: an amount in kmh, as an int or float.

    Returns:
        The same amount in mph, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kmh_to_mph(2) == 2 * 0.621371
    """
    return _check(value) * 0.621371


def mph_to_kmh(value):
    """Convert mph to kmh.

    Args:
        value: an amount in mph, as an int or float.

    Returns:
        The same amount in kmh, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: mph_to_kmh(2) == 2 * 1.609344
    """
    return _check(value) * 1.609344


def ms_to_kmh(value):
    """Convert ms to kmh.

    Args:
        value: an amount in ms, as an int or float.

    Returns:
        The same amount in kmh, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: ms_to_kmh(2) == 2 * 3.6
    """
    return _check(value) * 3.6


def kmh_to_ms(value):
    """Convert kmh to ms.

    Args:
        value: an amount in kmh, as an int or float.

    Returns:
        The same amount in ms, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kmh_to_ms(2) == 2 * 0.2777777777777778
    """
    return _check(value) * 0.2777777777777778


def joules_to_calories(value):
    """Convert joules to calories.

    Args:
        value: an amount in joules, as an int or float.

    Returns:
        The same amount in calories, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: joules_to_calories(2) == 2 * 0.239006
    """
    return _check(value) * 0.239006


def calories_to_joules(value):
    """Convert calories to joules.

    Args:
        value: an amount in calories, as an int or float.

    Returns:
        The same amount in joules, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: calories_to_joules(2) == 2 * 4.184
    """
    return _check(value) * 4.184


def kwh_to_joules(value):
    """Convert kwh to joules.

    Args:
        value: an amount in kwh, as an int or float.

    Returns:
        The same amount in joules, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kwh_to_joules(2) == 2 * 3600000.0
    """
    return _check(value) * 3600000.0


def joules_to_kwh(value):
    """Convert joules to kwh.

    Args:
        value: an amount in joules, as an int or float.

    Returns:
        The same amount in kwh, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: joules_to_kwh(2) == 2 * 2.7777777777777776e-07
    """
    return _check(value) * 2.7777777777777776e-07


def watts_to_horsepower(value):
    """Convert watts to horsepower.

    Args:
        value: an amount in watts, as an int or float.

    Returns:
        The same amount in horsepower, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: watts_to_horsepower(2) == 2 * 0.00134102
    """
    return _check(value) * 0.00134102


def horsepower_to_watts(value):
    """Convert horsepower to watts.

    Args:
        value: an amount in horsepower, as an int or float.

    Returns:
        The same amount in watts, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: horsepower_to_watts(2) == 2 * 745.7
    """
    return _check(value) * 745.7


def pascals_to_psi(value):
    """Convert pascals to psi.

    Args:
        value: an amount in pascals, as an int or float.

    Returns:
        The same amount in psi, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: pascals_to_psi(2) == 2 * 0.000145038
    """
    return _check(value) * 0.000145038


def psi_to_pascals(value):
    """Convert psi to pascals.

    Args:
        value: an amount in psi, as an int or float.

    Returns:
        The same amount in pascals, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: psi_to_pascals(2) == 2 * 6894.76
    """
    return _check(value) * 6894.76


def bar_to_psi(value):
    """Convert bar to psi.

    Args:
        value: an amount in bar, as an int or float.

    Returns:
        The same amount in psi, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: bar_to_psi(2) == 2 * 14.5038
    """
    return _check(value) * 14.5038


def psi_to_bar(value):
    """Convert psi to bar.

    Args:
        value: an amount in psi, as an int or float.

    Returns:
        The same amount in bar, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: psi_to_bar(2) == 2 * 0.0689476
    """
    return _check(value) * 0.0689476


def acres_to_hectares(value):
    """Convert acres to hectares.

    Args:
        value: an amount in acres, as an int or float.

    Returns:
        The same amount in hectares, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: acres_to_hectares(2) == 2 * 0.404686
    """
    return _check(value) * 0.404686


def hectares_to_acres(value):
    """Convert hectares to acres.

    Args:
        value: an amount in hectares, as an int or float.

    Returns:
        The same amount in acres, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: hectares_to_acres(2) == 2 * 2.47105
    """
    return _check(value) * 2.47105


def sqm_to_sqft(value):
    """Convert sqm to sqft.

    Args:
        value: an amount in sqm, as an int or float.

    Returns:
        The same amount in sqft, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: sqm_to_sqft(2) == 2 * 10.7639
    """
    return _check(value) * 10.7639


def sqft_to_sqm(value):
    """Convert sqft to sqm.

    Args:
        value: an amount in sqft, as an int or float.

    Returns:
        The same amount in sqm, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: sqft_to_sqm(2) == 2 * 0.092903
    """
    return _check(value) * 0.092903


def bytes_to_kib(value):
    """Convert bytes to kib.

    Args:
        value: an amount in bytes, as an int or float.

    Returns:
        The same amount in kib, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: bytes_to_kib(2) == 2 * 0.0009765625
    """
    return _check(value) * 0.0009765625


def kib_to_bytes(value):
    """Convert kib to bytes.

    Args:
        value: an amount in kib, as an int or float.

    Returns:
        The same amount in bytes, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kib_to_bytes(2) == 2 * 1024
    """
    return _check(value) * 1024


def mib_to_kib(value):
    """Convert mib to kib.

    Args:
        value: an amount in mib, as an int or float.

    Returns:
        The same amount in kib, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: mib_to_kib(2) == 2 * 1024
    """
    return _check(value) * 1024


def kib_to_mib(value):
    """Convert kib to mib.

    Args:
        value: an amount in kib, as an int or float.

    Returns:
        The same amount in mib, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kib_to_mib(2) == 2 * 0.0009765625
    """
    return _check(value) * 0.0009765625


def gib_to_mib(value):
    """Convert gib to mib.

    Args:
        value: an amount in gib, as an int or float.

    Returns:
        The same amount in mib, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: gib_to_mib(2) == 2 * 1024
    """
    return _check(value) * 1024


def mib_to_gib(value):
    """Convert mib to gib.

    Args:
        value: an amount in mib, as an int or float.

    Returns:
        The same amount in gib, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: mib_to_gib(2) == 2 * 0.0009765625
    """
    return _check(value) * 0.0009765625


def degrees_to_radians(value):
    """Convert degrees to radians.

    Args:
        value: an amount in degrees, as an int or float.

    Returns:
        The same amount in radians, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: degrees_to_radians(2) == 2 * 0.0174533
    """
    return _check(value) * 0.0174533


def radians_to_degrees(value):
    """Convert radians to degrees.

    Args:
        value: an amount in radians, as an int or float.

    Returns:
        The same amount in degrees, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: radians_to_degrees(2) == 2 * 57.2958
    """
    return _check(value) * 57.2958


def hours_to_minutes(value):
    """Convert hours to minutes.

    Args:
        value: an amount in hours, as an int or float.

    Returns:
        The same amount in minutes, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: hours_to_minutes(2) == 2 * 60
    """
    return _check(value) * 60


def minutes_to_hours(value):
    """Convert minutes to hours.

    Args:
        value: an amount in minutes, as an int or float.

    Returns:
        The same amount in hours, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: minutes_to_hours(2) == 2 * 0.016666666666666666
    """
    return _check(value) * 0.016666666666666666


def days_to_hours(value):
    """Convert days to hours.

    Args:
        value: an amount in days, as an int or float.

    Returns:
        The same amount in hours, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: days_to_hours(2) == 2 * 24
    """
    return _check(value) * 24


def hours_to_days(value):
    """Convert hours to days.

    Args:
        value: an amount in hours, as an int or float.

    Returns:
        The same amount in days, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: hours_to_days(2) == 2 * 0.041666666666666664
    """
    return _check(value) * 0.041666666666666664


def weeks_to_days(value):
    """Convert weeks to days.

    Args:
        value: an amount in weeks, as an int or float.

    Returns:
        The same amount in days, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: weeks_to_days(2) == 2 * 7
    """
    return _check(value) * 7


def days_to_weeks(value):
    """Convert days to weeks.

    Args:
        value: an amount in days, as an int or float.

    Returns:
        The same amount in weeks, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: days_to_weeks(2) == 2 * 0.14285714285714285
    """
    return _check(value) * 0.14285714285714285


def nautical_miles_to_km(value):
    """Convert nautical miles to km.

    Args:
        value: an amount in nautical miles, as an int or float.

    Returns:
        The same amount in km, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: nautical_miles_to_km(2) == 2 * 1.852
    """
    return _check(value) * 1.852


def km_to_nautical_miles(value):
    """Convert km to nautical miles.

    Args:
        value: an amount in km, as an int or float.

    Returns:
        The same amount in nautical miles, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: km_to_nautical_miles(2) == 2 * 0.539957
    """
    return _check(value) * 0.539957


def yards_to_m(value):
    """Convert yards to m.

    Args:
        value: an amount in yards, as an int or float.

    Returns:
        The same amount in m, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: yards_to_m(2) == 2 * 0.9144
    """
    return _check(value) * 0.9144


def m_to_yards(value):
    """Convert m to yards.

    Args:
        value: an amount in m, as an int or float.

    Returns:
        The same amount in yards, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: m_to_yards(2) == 2 * 1.09361
    """
    return _check(value) * 1.09361


def stones_to_kg(value):
    """Convert stones to kg.

    Args:
        value: an amount in stones, as an int or float.

    Returns:
        The same amount in kg, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: stones_to_kg(2) == 2 * 6.35029
    """
    return _check(value) * 6.35029


def kg_to_stones(value):
    """Convert kg to stones.

    Args:
        value: an amount in kg, as an int or float.

    Returns:
        The same amount in stones, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kg_to_stones(2) == 2 * 0.157473
    """
    return _check(value) * 0.157473


def tons_to_kg(value):
    """Convert tons to kg.

    Args:
        value: an amount in tons, as an int or float.

    Returns:
        The same amount in kg, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: tons_to_kg(2) == 2 * 1000
    """
    return _check(value) * 1000


def kg_to_tons(value):
    """Convert kg to tons.

    Args:
        value: an amount in kg, as an int or float.

    Returns:
        The same amount in tons, as a float.

    Raises:
        TypeError: if value is not a number.
        ValueError: if value is infinite or NaN.

    Example: kg_to_tons(2) == 2 * 0.001
    """
    return _check(value) * 0.001


def celsius_to_fahrenheit(value):
    """Convert degrees Celsius to degrees Fahrenheit."""
    return _check(value) * 9 / 5 + 32


def fahrenheit_to_celsius(value):
    """Convert degrees Fahrenheit to degrees Celsius."""
    return (_check(value) - 32) * 5 / 9
