#Instructions for building the Kosibah Accounts import and processing
## Goal : Import accounting inputs, process them and add them to an Excel spreadsheet
On a hourly basis we will scan source Dropbox folders for bank statements, receipts and invoices. We'll import them into ~/claude-coding/kosaccounts/imports and put them in the right directories. I will do this with a cron job to start with but we might move over to webhooks at some point the in the future in order to process this as an event drive activity. We'll use rclone to import the documents we'll be supplied.
Once a new file is detected, we'll need to process it. Processing varies depending on the type of document that it is: 
1. Receipts
2. Bank statements
3. Invoices
These will all be in different folders.

## Handling the Kosibah purchases and receipts.xlsx file
This is the master file containing all the purchasing and invoicing history for Kosibah Creations Ltd. It has four worksheets within it:
1. Cover sheet - ignore this, we use it for printing statements each quarter
2. Purchases - a list of all the inputs to the business. The fields there are:
  1. Date (in YYYY-MM-DD format)
  2. Company (this is the supplier and we should normalise to make sure we're not creating duplicate suppliers)
  3. Category - category can be derived from previous purchases from the same supplier but may have to be guessed
  4. Schedule C - this is a lookup to the IRS tax categories for our annual return. We usually need a bit more detail under these.
  5. Net amount paid.
  6. Sales tax amount (usually 8.875% because we're buying in New York)
  7. Total amount - same of (5) and (6).
  8. Payment method
  9. Check - calculated by Excel to make sure that Total is correct.
  10. Notes - any thing we want to flag
  11. Year - calculated from Date in order to extract a year's transactions for tax purposes.
3. Receipts - we'll leave this alone for the time being. A future enhancement might introduce processing here.
4. Lookup values - you can ignore this

### Supplier's list and categories
One of the things that the process has to do is extract a running list of all the distinct suppliers (and suggest where there might be duplications). A supplier should have a default category associated for purchases. We shouldn't introduce new categories without proper consultation.

## Processing
We are going to process each batch of files as they come in (they'll usually come in batches because that's how we feed them).
1. Receipts - Claude will read the receipt (which could be in a variety of formats - structured pdf, image) and identify the key values for inputting into the spreadsheet: date, suppplier, category, amount, sales tax. Typically the file name will have the date encoded as YYYY-MM-DD - supplier name. That information should be added to the bottom of the Receipts worksheet. You could add a note when it's been completed.
2. Bank statements. These should be fairly easy to process as they will come in html format. It should be possible to process them using python scripts. The goal then is to identify any receipt transactions that occur only as bank withdrawals (there are a few) and reconcile any transactions that either don't have a corresponding bank entry or don't have a corresponding receipt. We can note that in the Notes column, perhaps.

## Some important considerations:
- We want this to run continuously - at least as often as a new import arrives throught the rclone process.
- We should be appending to the Excel file. My role at the end of the quarter will be to take the Excel file and import its entries straightforwardly into my master spreadsheet - it will probably be helfpul to see what date the entries were processed by Claude - you can adapt the spreadsheet to cover that. While will also help to reconcile with the log of entries.
