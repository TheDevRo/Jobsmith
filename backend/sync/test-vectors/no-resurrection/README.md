# Vector: no-resurrection

Proves tombstones win or lose purely on timestamp, not by being "sticky".

| id                   | versions                                             | winner        |
|----------------------|------------------------------------------------------|---------------|
| `job greenhouse:100` | A create @10:00, C create @12:00, A **delete @14:00**| delete → tombstone (older create must NOT resurrect it) |
| `job lever:200`      | A **delete @10:00**, C create @15:00                 | create → live (newer edit revives a deleted record)     |

The failure this guards against: an engine that treats any tombstone as final
would wrongly keep `lever:200` deleted, or one that lets any create override a
tombstone would wrongly resurrect `greenhouse:100`.
