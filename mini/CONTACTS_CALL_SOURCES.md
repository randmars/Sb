# Where every Contacts call and key name comes from

The Contacts helper (`switchboard_mini/contacts_transport.py`) is JavaScript for JXA, and it is
allowed to name only symbols that a record in the Gate 2 reference pack -- or Apple's own
documentation, when the pack is silent about a symbol -- names. `CALL_SOURCES` and
`KEY_SOURCES` in that module are the machine-readable copy of this table; the probe carries the
same strings into each capability row's `evidence`, so a row can be traced to a page.
`tests/test_mini_contacts.py::ContactsCallSourceTests` fails if a name used in the script has no
source, if a source does not name a URL, or if a "not documented in the pack" entry does not say
so in the words "undocumented"/"respondsToSelector".

The pack records are in `/home/team/shared/probe-pack/` (`O13`, `O14`, `O15`, `O16`).

| Name the script uses | Source |
| --- | --- |
| `CNContactStore` | pack O13 -- https://developer.apple.com/documentation/contacts |
| `CNContactStore.authorizationStatusForEntityType:` | Apple docs (not in the pack -- no read page names a status query) -- https://developer.apple.com/documentation/contacts/cncontactstore/authorizationstatus(for:) |
| `CNContactStore.requestAccessForEntityType:completionHandler:` | pack O13 -- https://developer.apple.com/documentation/contacts |
| `CNEntityTypeContacts` | Apple docs -- https://developer.apple.com/documentation/contacts/cnentitytype |
| `CNAuthorizationStatus*` (five cases) | Apple docs -- https://developer.apple.com/documentation/contacts/cnauthorizationstatus |
| `CNContactFetchRequest` | pack O13 -- https://developer.apple.com/documentation/contacts |
| `CNContactFetchRequest.setShouldUnifyResults:` | **undocumented in the pack** -- https://developer.apple.com/documentation/contacts; the helper asks the installed object `respondsToSelector` before using it (O13 procedure step 4 says to take the property's exact name from the installed SDK header) |
| `CNContactStore.enumerateContactsWithFetchRequest:error:usingBlock:` | **undocumented in the pack** -- https://developer.apple.com/documentation/contacts; checked with `respondsToSelector` first |
| `CNContactStore.enumeratorForChangeHistoryFetchRequest:error:` | pack O15 -- https://developer.apple.com/documentation/technotes/tn3149-fetching-change-history-events |
| `CNChangeHistoryFetchRequest` (+ `shouldUnifyResults`, `includeGroupChanges`, `mutableObjects`, `startingToken`) | pack O15 -- https://developer.apple.com/documentation/technotes/tn3149-fetching-change-history-events |
| `CNFetchResult.value`, `.currentHistoryToken` | pack O15 -- https://developer.apple.com/documentation/technotes/tn3149-fetching-change-history-events |
| `CNContactIdentifierKey` | pack O15 -- https://developer.apple.com/documentation/technotes/tn3149-fetching-change-history-events |
| `CNContactGivenNameKey`, `CNContactFamilyNameKey`, `CNContactEmailAddressesKey`, `CNContactPhoneNumbersKey` | pack O13 procedure step 2's minimal key list -- https://developer.apple.com/documentation/contacts |

The notes-guarded key (`CNContactNoteKey`, used only by `contacts restricted-keys`) is
deliberately **not** in this table: no page read in the pack names it, so the worker never
supplies it itself. The owner passes a symbol from the installed SDK header
(`--key-symbol`), and the row reports whether the installed build defines it.

Nothing here is a capability claim. A row built from these names is only ever a *measurement*
when a real Mac answered it; in fixture mode every Contacts row is `supported: false` and
labelled `FIXTURE:contacts(...)`.
