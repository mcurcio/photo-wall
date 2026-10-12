"""The Schedule page's parts, located by their accessible names (a page object).

Every name comes from the console's own words (console_words.py, read from showWords.json), so
a test says *which* part it wants (a Schedule's card, its Edit button, the flow's form) and never
re-types the console's copy. The Schedule flow's tasks (start it, walk it, make a Schedule) are in
console_tasks.py and use these.
"""

from support.console_words import SCHEDULE, schedule


def schedules(page):
    """The Schedule page's region: the Schedule cards and the Schedule flow."""
    return page.get_by_role("region", name=SCHEDULE["region"], exact=True)


def card(page, name):
    """Schedule `name`'s card."""
    return schedules(page).get_by_label(schedule("card", name=name), exact=True)


def new_button(page):
    """The button that opens a new Schedule in the flow."""
    return schedules(page).get_by_role("button", name=SCHEDULE["newButton"], exact=True)


def create_form(page):
    """The Schedule flow's form for a new Schedule (every step renders it)."""
    return schedules(page).get_by_role("form", name=SCHEDULE["createForm"], exact=True)


def edit_form(page, name):
    """The Schedule flow's form while it edits Schedule `name`."""
    return schedules(page).get_by_role("form", name=schedule("editForm", name=name), exact=True)


def edit_button(scope, name):
    """Schedule `name`'s Edit, inside `scope` (its card, or the page)."""
    return scope.get_by_role("button", name=schedule("editButton", name=name), exact=True)


def remove_button(scope, name):
    """Schedule `name`'s Remove, inside `scope` (its card, or the page)."""
    return scope.get_by_role("button", name=schedule("removeButton", name=name), exact=True)


def remove_dialog(page, name):
    """The confirmation that removes Schedule `name` while its window plays."""
    return page.get_by_role("dialog", name=schedule("removeDialog", name=name), exact=True)


def save_button(form):
    """The flow's write on Review (a new Schedule's and an edit's)."""
    return form.get_by_role("button", name=SCHEDULE["save"], exact=True)


def name_field(form):
    """Review's name field."""
    return form.get_by_label(SCHEDULE["nameField"], exact=True)


def said(page):
    """What the Schedule page says after a write (its status line)."""
    return schedules(page).get_by_role("status")
