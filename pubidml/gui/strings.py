"""Every word the window says, in one place.

Dutch is the only language, so there is no gettext and no locale
detection -- building that machinery before a second language is asked
for is work spent on a guess. What matters is that the strings are
together, so adding a second language later is an addition rather than a
rewrite.

Dutch runs roughly a fifth longer than English, so anything laid out
against these must size itself from them rather than from a mock.
"""

WINDOW_TITLE = "Publisher-bestanden omzetten"

# --- Step 1: kiezen -------------------------------------------------
STEP1_TITLE = "Wat wilt u omzetten?"
STEP1_DROP_HINT = (
    "Sleep een map op het pictogram van dit programma,\n"
    "of kies hieronder wat u wilt omzetten."
)
STEP1_CHOOSE_FOLDER = "Map kiezen…"
STEP1_CHOOSE_FILES = "Bestanden kiezen…"
STEP1_FOUND = "{files} Publisher-bestanden gevonden in {folders} mappen."
STEP1_FOUND_ONE = "1 Publisher-bestand gevonden."
STEP1_NONE = (
    "Hier staan geen Publisher-bestanden (.pub).\n"
    "Kies een andere map."
)

STEP1_MIXED_ROOTS = (
    "Deze bestanden staan op verschillende schijven.\n"
    "Kies bestanden uit één map, of kies een map."
)

# --- Step 2: bestemming ---------------------------------------------
STEP2_TITLE = "Waar moeten de omgezette bestanden komen?"
STEP2_SAVE_TO = "Opslaan in:"
STEP2_CHANGE = "Wijzigen…"
STEP2_STRUCTURE = "De mappenstructuur wordt hierin overgenomen."
STEP2_INSIDE_SOURCE = (
    "Deze map ligt binnen de map die u omzet. Kies een map die\n"
    "daarbuiten ligt, anders zet een volgende keer het programma\n"
    "zijn eigen uitvoer opnieuw om."
)

# --- Step 3: bezig ---------------------------------------------------
STEP3_TITLE = "Bezig met omzetten…"
STEP3_PROGRESS = "{done} van {total}"
STEP3_CANCEL = "Annuleren"
STEP3_CANCELLING = "Bezig met stoppen…"

# --- Step 4: klaar ---------------------------------------------------
STEP4_TITLE = "Klaar — {done} van de {total} bestanden"
STEP4_TITLE_CANCELLED = "Gestopt — {done} van de {total} bestanden"
STEP4_OK = "{n} goed omgezet"
STEP4_REVIEW = "{n} even controleren"
STEP4_FAILED = "{n} konden niet gelezen worden"
STEP4_SKIPPED = "{n} overgeslagen (waren al omgezet)"
STEP4_NEXT = (
    "Hierna: open elk .idml-bestand in Affinity en kies\n"
    "Bestand → Opslaan als… Laat de map _images ernaast staan\n"
    "totdat u dat gedaan heeft."
)
STEP4_OPEN_FOLDER = "Map openen"
STEP4_OPEN_REPORT = "Rapport openen"
STEP4_AGAIN = "Nog een map omzetten"
# Shown in place of the report button when there is no report to open.
# Without it the button would simply do nothing when pressed, which is
# the one thing this window tries never to do.
STEP4_NO_REPORT = (
    "Er is geen rapport geschreven. Kijk in het logbestand\n"
    "wat er misging."
)

# --- Navigation ------------------------------------------------------
BACK = "Vorige"
NEXT = "Volgende"
START = "Omzetten"
CLOSE = "Sluiten"

# --- Errors ----------------------------------------------------------
ERROR_SEE_REPORT = "zie het rapport"
ERROR_UNEXPECTED = (
    "Er ging iets mis wat het programma niet verwachtte.\n"
    "Het logbestand staat in:\n{path}"
)
