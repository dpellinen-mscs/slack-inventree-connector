# Modeling vehicles and sites as stock locations

The connector moves stock between InvenTree stock locations, and only between those. It never
creates a location by itself. Every place your team names in a post (a shelf, a vehicle, a job
site) has to be a stock location before a movement there can be read as one. This page is for the
admin who sets that up. All names below are examples.

## A layout that works

```text
Warehouse                (structural)
├── Main Stock
└── Receiving
Vehicles                 (structural)
├── Truck 1              description: Sam's truck
└── Van 2                description: Luis's van
Sites                    (structural)
└── Smith job            description: Install on Elm Street
```

- **Folders are structural.** In InvenTree, a structural location groups other locations and
  cannot hold stock. The connector never matches a structural location, so "Vehicles" or "Sites"
  can never be the destination of a movement by mistake.
- **One location per vehicle,** under a folder of its own. Stock on a vehicle is then visible in
  InvenTree, and moving it back is a transfer like any other.
- **One location per site or job** that stock goes to and stays at. A `take_to_site` movement is a
  transfer to that location. A site that is no longer used can be made structural once it holds no
  stock (InvenTree refuses while it does). From then on it is never matched.

## How the words of a post find a location

The matcher compares the place a post names with each location's name, path and description, in a
fixed order. The first rule with a hit decides, and the model never picks the location.

1. **Exact:** the words are the location's name or its whole path ("Truck 1", "Vehicles/Truck 1").
2. **Normalized:** the same after case, punctuation, possessives and plurals are folded
   ("main stock" finds "Main Stock").
3. **Alias:** a confirmed alias, or "my truck" (below).
4. **Words:** every word of the place is a word of one location's path or description. "Sam's
   truck" finds Truck 1 through its description, and "Elm Street" finds the Smith job.
5. **Fuzzy:** as words, but a word of four letters or more may have one typo ("recieving").

Words with digits must match exactly: "truck 7" never becomes Truck 1. When more than one location
fits, the dry run lists them, and the question in the thread offers them as buttons. "Truck"
alone, with two trucks, is such a case. When nothing fits, the question asks which location was
meant.

What this means for names:

- **Name places the way people talk about them,** and put the other ways in the description.
  Words in the description count as much as words in the name.
- **Watch shared words.** In the layout above, "the warehouse" fits both Main Stock and Receiving,
  because both paths contain "Warehouse", so it is asked as a choice every time. If your team says
  "the warehouse" for one place, make that place the location named "Warehouse" (not structural,
  holding the stock). Then put Receiving beside it or below it: a name that fits wins before any
  words are compared.
- **Keep digits and letters distinct.** "Truck 1" and "Truck 11" are told apart; "Truck A" and
  "Truck A2" are too.

## Vehicles and "my truck"

"My truck", "my van", "my vehicle", "my car" and "my trailer" mean the vehicle of the person who
posted. The connector learns whose vehicle is whose from the `[vehicles]` section of
`stockthread.toml`:

```toml
[vehicles]
U0SAM0001 = "Vehicles/Truck 1"
U0LUIS0005 = "Vehicles/Van 2"
```

- The key is the person's Slack member id. In Slack, open their profile, choose *More* (⋮), then
  *Copy member ID*. Display names are never used: anyone can change theirs.
- The value is the location's whole path, letter for letter, as InvenTree shows it. A path that is
  not found leaves that person without a vehicle, and the connector names them in its log when it
  reads the catalog. A near miss is never guessed.
- The word has to fit the vehicle. "My van" finds Van 2 for Luis, but not Truck 1 for Sam, unless
  "van" is a word of Truck 1's path or description. A person who says both can have both words in
  the description.
- If you rename or move a vehicle's location in InvenTree, update its path here too. Until you do,
  "my truck" for that person is unmatched, and their posts ask which location was meant.

A person without a vehicle can still name it ("Truck 1", "Sam's truck"). Only "my truck" needs the
mapping. When it is missing, "my truck" matches no location: the dry run says so, and the question
in the thread asks which stock location "my truck" is.

## Where stock comes from

Many posts name only where stock went: "2 Disc 40 on my truck". The connector does not assume a
source. The dry run shows "No source location was stated", and the question asks for it. A default
source is a policy setting that may come later. Until then, a post that names both places ("from
main stock to my truck") is the one that needs no question.

## Checklist

1. Create the structural folders, then a location for each stock room, vehicle and site.
2. Add the nicknames people use to each location's description.
3. Map each person who says "my truck" in `[vehicles]`, and run `.venv/bin/stockthread check-config`.
4. Start the connector, and read its log for people whose vehicle path was not found.
5. Post a test movement in the channel, and read the dry run: it names the location it matched next
   to the words as written.
