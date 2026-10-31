"""Tunable download settings.

Edit the values in this file. They are not read from the environment.
"""

# Instruments per quote() call (`GET /quote?i=EXCHANGE:SYMBOL&i=...`).
# Kite allows 500, but F&O symbols are long: 500 keys make a ~15KB URL, and
# the API edge rejects that with an HTML "400 Bad request". 250 stays near ~8KB.
# Lower this if that 400 comes back; raising it risks the same rejection.
QUOTE_BATCH_SIZE = 250
