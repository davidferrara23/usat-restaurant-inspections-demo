# Restaurant inspections

## Workflow

```mermaid
flowchart TB
    1[User provides a list of cities, counties or states of interest.]
    2{Scraper Builder agent}
    3[Scraper Builder agent iterates to develop scraping scripts.]
    4["Scripts are human-reviewed for accuracy."]
    5["Scripts are added to a library for manual runs (dev) or to Redbird (production)."]

subgraph First run to create scrapers
1-->2
2-->3
3-->4
4-->5
end
```

```mermaid
---
title: Subsequent runs
---
flowchart TB
    5((Latest restaurant inspections obtained by scrapers))
    6[(Restaurant inspection database)]
    7[/Restaurant analysis\]
    7a((Number of reviews on Yelp or other sites))
    7aa{{If greater than average, add 1 point}}    
    7b2((Previous coverage))    
    7ba((Number of mentions))    
    7baa{{If prior coverage mentions are greater than average, add 1 point}}
    7bb{Sentiment agent}
    7bba{{If prior coverage sentiment is deemed overly positive or overly negative, add 1 point}}                      
    7c((Severity of complaints))
    7ca{Sentiment agent}
    7cb{{If complaints are determined to be severe, add 1 point}}
    8{{If index is greater than 3, send tip. Otherwise, end.}}                                            
    9{Tip Sheeter agent}
    10[Tip sheet generated with brief 5 Ws and newsworthiness of recent restaurant inspections.]
    9a{Espresso}
    9aa[Presto asset created with brief information and breakdown of recent notable restaurant inspections.]
    11["Tip sheet sent to appropriate reporter(s) by email."]
5-->6
6-->7
7-->7a
7-->7b2
7-->7c
7a-->7aa
subgraph Content API
7b2-->7ba
7ba-->7baa
7bb-->7bba
7b2-->7bb
end
7c-->7ca
7ca-->7cb
7aa-->8
7cb-->8
7baa-->8
7bba-->8
8-->9
8-->9a
9a-->9aa
9-->10
10-->11    
```

## Database

In this demo, the database uses SQLite and FastAPI for easy testing with Copilot Studio.

In production, the data would be stored as part of a larger data pool for access by other scrapers and workflows. Redbird may also be factored in for deployment.

Three primary tables are constructed: 

* **restaurants**, which includes all necessary information about the individual establishments being scraped;
* **inspections**, which includes each individual inspection scraped by the tool; and
* **target reporters**, a proxy table to fill in for a tool that identifies the appropriate reporter a tip should be routed toward.

```mermaid
classDiagram
    class Restaurants{
    id
    name
    address   
    city
    state
    county
    newsroom
    lastInspected
    lastInspectionId       
    lastUpdated
    yelpReviewsCount
    yelpCuisine   
    priorCoverage                                                        
    }

    class Inspections{
    id
    restaurantId
    score
    grade
    date
    details                  
    }

    class TargetReporters{
    id
    name
    email
    beat
    market
    state                      
    }
```

## Tip Sheeter agent schema

```mermaid
flowchart TD
  1[A restaurant inspection meets the criteria for a tip to be sent.]
  2[Server sends payload to Tip Sheeter agent flow.]
  3[Flow triggers the Tip Sheeter agent.]
  4[Tip Sheeter agent generates a tip sheet based off the provided payload information.]
  5[Flow sends AI-generated tip sheet to designated reporter in payload.]    

1-->2
2-->3
3-->4
4-->5
```
