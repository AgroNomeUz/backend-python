# Demo data

Mock accounts, machines and listings for demos, loaded by

```
python manage.py seed_regions   # once, if the regions aren't there yet
python manage.py seed_demo      # safe to re-run
python manage.py seed_demo --reset   # remove everything seed_demo created
```

Every account logs in with its phone number and the password `mockuser`.

| File | Contents |
|---|---|
| `catalog.json` | Equipment categories, manufacturers and the 16 `EquipmentModel` rows every listing points at. `photo` names the listing photo used for that model; `requires_hitch` becomes an `EquipmentModelCompatibility` row. |
| `regions/<code>.json` | One file per region (`code` = `Region.code`, lowercased): 181 accounts and 634 listings in total. Each account becomes a user, the organization it owns, and one asset + one active listing per listing entry. |
| `photos/` | One photo per catalog model, resized to 1024 px. Each listing gets its own copy under `media/listings/`, so deleting a demo listing's photo can't break another listing. |

Serial numbers (`AN-01-02-06`) and phone numbers are unique across all files;
`seed_demo` keys its upserts on them, so editing a file and re-running updates
the existing rows instead of duplicating them.

## Photo credits

Models 6, 7, 8 and 10 are from [Pexels](https://www.pexels.com/license/) (free to use, no attribution required):
[c06](https://www.pexels.com/photo/harvester-and-truck-on-field-19327950/),
[c07](https://www.pexels.com/photo/a-harvester-in-a-cotton-field-13924881/),
[c08](https://www.pexels.com/photo/tractor-plowing-the-field-19359398/),
[c10](https://www.pexels.com/photo/john-deere-hoe-seed-drill-an-agricultural-planting-machine-16407472/).

The rest are from Wikimedia Commons, under the licences listed:

| File | Author | Licence | Source |
|---|---|---|---|
| c01 | Globetrotter19 | CC BY-SA 4.0 | [Belarus 892.2, 2025 Dunapataj](https://commons.wikimedia.org/wiki/File:Belarus_892.2,_2025_Dunapataj.jpg) |
| c02 | Bene Riobó | CC BY-SA 4.0 | [John Deere 6155M 01](https://commons.wikimedia.org/wiki/File:John_Deere_6155M_01.jpg) |
| c03 | JoachimKohler-HB | CC BY-SA 4.0 | [Case IH Puma 175 CVXDrive](https://commons.wikimedia.org/wiki/File:Case_IH_Puma_175_CVXDrive.jpg) |
| c04 | TaurusEmerald | CC BY-SA 4.0 | [Kubota M7060 Sonoma 2026](https://commons.wikimedia.org/wiki/File:Kubota_M7060_Sonoma_2026.jpg) |
| c05 | Pierre André Leclercq | CC BY-SA 4.0 | [Claas Tucano 450 in Godewaersvelde](https://commons.wikimedia.org/wiki/File:Claas_Tucano_450_in_Godewaersvelde.jpg) |
| c09 | W.carter | CC0 | [Disc harrow on Röe Gård 2](https://commons.wikimedia.org/wiki/File:Disc_harrow_on_R%C3%B6e_G%C3%A5rd_2.jpg) |
| c11 | FendtPower | CC BY-SA 4.0 | [Fendt Vario 410 mit Horsch Grubber](https://commons.wikimedia.org/wiki/File:Fendt_Vario_410_mit_Horsch_Grubber.jpg) |
| c12 | Lars Plougmann | CC BY-SA 2.0 | [Case IH tractor with Hardi field sprayer, Lolland](https://commons.wikimedia.org/wiki/File:Case_IH_tractor_with_Hardi_field_sprayer,_Lolland.jpg) |
| c13 | JoachimKohler-HB | CC BY-SA 4.0 | [New Holland T7.170 mit Krone Rundballenpresse](https://commons.wikimedia.org/wiki/File:New_Holland_T7.170_mit_Krone_Rundballenpresse.jpg) |
| c14 | Charles Knowles | CC BY 2.0 | [Corn field and tractor with trailer at sunset](https://commons.wikimedia.org/wiki/File:Corn_field_and_tractor_with_trailer_at_sunset.jpg) |
| c15 | BulldozerD11 | CC BY-SA 3.0 | [Manitou MLT 627 turbo](https://commons.wikimedia.org/wiki/File:Manitou_MLT_627_turbo_-_4749.jpg) |
| c16 | Grimme Group | CC BY 2.0 | [Grimme SE 260 trailed 2-row potato harvester](https://commons.wikimedia.org/wiki/File:Grimme_SE_260_trailed_2-row_potato_harvester_(19771803540).jpg) |

All photos were resized; none were otherwise altered.
