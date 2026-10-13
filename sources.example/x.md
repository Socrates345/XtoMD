// One X handle per line. "//" starts a comment ("#" still does too, but a line
// that opens with it shows as a big heading in Obsidian or on GitHub).
// Everything else is written variable:value, in "|" fields, in any order.
//
// After a handle:
//   name:Frank Wire   shown instead of @handle
//   priority:high     its tweets sort first and are shown in full (default: normal)
//   reposts:off       drops its plain reposts, one with its own comment is kept (default: on)
//   only:a, b         keeps only its tweets with one of these keywords
//   hide:a, b         drops its tweets with one of these keywords
// After a "## group" header, which splits the handles into sections:
//   level:high        how much of the group the brief keeps: high, normal or low (default: normal)
//   recap:on          a group only worth its overall picture; it ignores level (default: off)
//   reposts:off       for every handle of the group that does not say reposts:on
//   only:a, b         for every handle of the group, added to each handle's own
//   hide:a, b         the same
//
// A keyword counts from the start of a word, in any case: ad finds "ads" and
// "#ad", not "made". One that holds a #, a // or a comma goes in double quotes.
// Lines written the old way still load (handle | Display Name | priority, and
// "## group | recap" or "| high").

alice

## finance | level:high | hide:presale
NewsFrank | name:Frank Wire | priority:high
gina_alerts | reposts:off   // a comment can end a line too
jumper | only:airdrop, "#alpha"

## chatter | recap:on
dave
