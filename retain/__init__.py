"""retain: transitory email cleanup for a local-government archive.

Modules, in the order worth reading them:

    __main__   the `run` and `generate` commands; the product's control flow
    classify   parse, sensitivity regex, header rules, Ollama client, decision table
    store      SQLite state and audit trail
    report     decisions.csv (director edits) and summary.html (director reads)
    generate   synthetic demo archive; not part of the product

Standard library only.  The one network call is to Ollama on loopback.
"""
