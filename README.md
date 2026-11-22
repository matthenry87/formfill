# formfill

Fills web forms (doctor intake, check-in) in Chrome on the phone from an encrypted profile.
It drives Chrome through its DevTools socket over adb: one script fills every field at once.

## One-time setup
    formfill init        # opens the profile template in nano; save, then pick a passphrase
    formfill edit        # change it later

Put insurance card / ID photos in `~/formfill/files/` and list their paths under `files:` in the profile.

## At the doctor's office
1. Open the form link in Chrome.
2. `formfill unlock` (passphrase; the decrypted copy lives in RAM for 30 min, then auto-wipes)
3. Ask Claude to "fill it". Or, for a site that's already mapped, run `formfill fill` yourself.
4. Check the page: green = filled, red = couldn't fill, orange = required but still empty.
   Sign and submit yourself.
5. `formfill lock` wipes the decrypted profile and removes uploaded photos from the phone.

## Commands
| | |
|---|---|
| `formfill scan` | list fields on the current Chrome tab (labels/options only) |
| `formfill fill [--dry-run]` | fill using `maps/<host>.yaml` |
| `formfill keys` | list profile key names (no values) |
| `--tab <text>` | pick a tab by URL/title instead of the visible one |

## Maps (`maps/<host>.yaml`)
    fields:
      first_name: name.first                 # profile key
      dob: dob|MM/DD/YYYY                    # date reformatted
      full_name: "{name.first} {name.last}"  # template
      smoker: "=No"                          # fixed answer
      card_front: file:insurance_front       # upload
      signature: skip
Field keys come from `formfill scan` (element id, else name, else `label:<text>`).

## Limits
- Fields inside iframes (some embedded form builders) aren't reached yet.
- Custom JavaScript dropdowns that aren't a real `<select>` may need a manual tap.
- Port 5555 adb is open on any Wi-Fi the phone joins; only accept prompts you triggered.
