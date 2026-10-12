"""The week page's power parts, located by their accessible names (a page object).

Every name comes from the console's own words (support/console_words.py, read from
powerWords.json), so a test says which part it wants (All off, the Night off editor, a Night off
band on a day) and never types the console's copy again.
"""

import re

from support.console_words import POWER, power


def week(page):
    """The week page's region: the calendar, the Schedules and the rules."""
    return page.get_by_role("region", name="Week", exact=True)


def button(scope, key):
    """The power button whose words are POWER[`key`] ("allOff", "resume", "saveNightOff", …)."""
    return scope.get_by_role("button", name=POWER[key], exact=True)


def hold_banner(page):
    """The hold banner above the week, while a hold is in force."""
    return week(page).get_by_role("region", name="Holds", exact=True)


def hold_lasts(page):
    """The "A hold lasts" setting beside All off and All on."""
    return week(page).get_by_role("radiogroup", name=POWER["holdLasts"], exact=True)


def hold_lasts_choice(page, key):
    """The setting's choice whose words are POWER[`key`] ("holdLastsNext", "holdLastsResumed")."""
    return hold_lasts(page).get_by_role("radio", name=POWER[key], exact=True)


def night_off_editor(page):
    """The Night off editor on the week page."""
    return week(page).get_by_role("region", name=POWER["nightOff"], exact=True)


def band(page, day, name):
    """The band named `name` (a regex) in `day`'s row of the calendar."""
    return week(page).get_by_role("group", name=day, exact=True).get_by_role("img", name=re.compile(name))


def night_off_band(page, day, when):
    """`day`'s Night off band, whose span reads `when` ("Wed 14 Oct 23:00 – Thu 15 Oct 07:00")."""
    return band(page, day, "^" + re.escape(power("band", what=POWER["nightOffBand"], when=when)) + "$")


def power_line(words):
    """The Frame page's display power line saying `words`."""
    return power("powerLine", words=words)


def resting_line(until):
    """The power line of a display resting in Night off until `until` ("07:00")."""
    return power_line(power("until", words=power("restingFor", reason=POWER["nightOff"]), time=until))
