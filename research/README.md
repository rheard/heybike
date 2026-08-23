## Background

I like my e-bike. Its large and heavy but so am I, and its the only bike I've ever ridden where I don't feel hunched over. 
    However I had a problem after moving across the US recently: the battery did not survive the journey. 
    Nevertheless I was able to contact the company and purchase a replacement battery, hooray.

This got me thinking, is there other maintenance I should be doing? Perhaps a **firmware update**? First though lets talk more about the bike...

# The Bike and Company

I own a **Heybike Cityrun**. Heybike is an e-bike company headquartered out of [Farmer's Branch, Texas](https://www.linkedin.com/company/heybike)... 
    er, sorry, [San Francisco, CA](https://www.bbb.org/us/ca/san-francisco/profile/electric-bike/heybike-inc-1116-956794)... 
    er, sorry, [Ontario, CA](https://heybike.zendesk.com/hc/en-us/articles/37708193859993-6-Contact-US)... 
    er, sorry, [Shenzhen, China](https://www.theveloindex.com/spotlight/heybike-overview). 
Given that they are [sharing database credentials on WeChat](https://heybike.oss-us-west-1.aliyuncs.com/pic/20211111/00feb66f08a9450480d5e34e5406c85e.png), 
    this last one is the one I am inclined to believe the most; more on this later.

To the company's credit, providing replacement batteries is very nice.

## The App

As it turns out there is a way to do firmware updates for Heybikes, it is done via the [Heybike app](https://play.google.com/store/apps/details?id=com.yulai.heybike.new&hl=en_US). 
    I installed the app and surprise surprise, it is awful; Google auth is broken, its generally slow, looks bad... 
    Worst part is that the firmware update doesn't work. It gets to 1% then dies, freezing the bike and necessitating a power cycle. 
    I tried it several times and the behavior was consistent.

I've been to several talks at DEFCON over the years where an entry point into devices was through a mobile app 
    (specifically [this one](https://www.youtube.com/watch?v=AfMfYOUYZvc) and [this one](https://www.youtube.com/watch?v=caY7ls4G460) come to mind). 
    I personally have never looked at a mobile app before; I've only ever done PC or Xbox executables. 
    If there was ever a mobile app I wanted to reverse engineer though, this is the one. I hate this app.

# Reverse Engineering

Here is where we get to the AI: Thankfully I live in 2026 and I can just ask ChatGPT. 
    Specifically I explained that I wanted to reverse engineer an android app and needed to know what is the ghidra or IDA pro of reverse engineering android apps. 
    ChatGPT pointed me to [jadx-gui](https://github.com/skylot/jadx), a nice tool which does exactly what I was imagining: allow me to get Java code from the compiled APK.

Now I just needed to comb through the generated Java code... or do I? Again, I live in 2026. 
    I can just export it all to a folder and load it up with IntelliJ, where I have the OpenAI Codex plugin!

I thought I challenged codex with too much right off the bat. The first prompt I gave it was: 
    "I know Python, I'll be able to read Python code. I know the app can easily connect to my bike and turn it on or off. 
    Please write me a Python script which turns my bike on or off, which is easy to use, and that I can read to understand."

However, that was not a problem for Codex at all.

The first thing it did was create a document detailing how Bluetooth communication works (linked [here](https://github.com/rheard/heybike/blob/main/research/PROTOCOL_OPCODE_MAP.md) after further edits). 
    Essentially it uses AES-encrypted BLE with a `0x6162` magic header followed by an opcode, and then variable-size payload for opcode data. 
    There are various opcodes available, for instance the opcode we are interested in for power is `0x31` , with a `0x01` payload as on and `0x00` payload as off. Simple.

With just this it was able to complete [the Python script](https://github.com/rheard/heybike/blob/main/research/scripts/heybike_power.py). 
    Well, almost. I had to pull some keys from a config on my phone, but once entered into the global variables the AI provided, 
    the script amazingly just... worked. It takes in my heybike credentials, gets my bike information from my profile, 
    then crafts the power state payload, encrypts and sends it to the bike with bleak.

I was a bit taken aback here. Going from "I think I want to reverse engineer this app" to "I've mapped out BLE communication, 
    reverse engineered all the opcodes, and created a Python script that can turn my bike off" took approximately one hour with ChatGPT and Cortex. 
    Doing all of this while also learning to reverse engineer Android code in the first place, without a lot of free time... 
    that could have taken a week at least a couple years ago, and that's if I was motivated.

I also asked cortex to create a similar document detailing the various API endpoints used by the app, 
    along with their data structures going in/out (linked [here](https://github.com/rheard/heybike/blob/main/research/APP_ENDPOINTS.md) after further revision). 
    Between the two linked documents, that provides essentially everything one needs to completely replace the app.

## Encryption

Before we discuss the firmware update process, lets talk more about the BLE encryption.

I mentioned the script pulls my bike information from my profile and connects to it. 
    Well thats because the endpoint for this (`getUseBikes`) provides everything needed directly from a user token: 
    the BLE key, the BLE mac, and the encryption key.

There are other endpoints for getting the encryption key though. For instance there is `getBikeByIMEI` and `getBikeBleKey`.

I asked cortex: "I suspect that these endpoints are not verifying that the bike being requested belongs to the account linked to the associated token. 
    I'd like you to update the on/off script to accept any random credentials, and attempts to use these endpoints to turn a nearby bike off. 
    You should find a nearby bike's BLE information using bleak itself instead of the endpoints."

To OpenAI's ever minute credit, Cortex told me it would not do this. It said a script like that could be used for hacking. 
    However it would write me a script which would accept my credentials, and the credentials of a test account which I also own, 
    and try to use these endpoints to connect to a bike which I do own with a test account that doesn't own it, to validate them (linked [here](https://github.com/rheard/heybike/blob/main/research/scripts/heybike_acl_check.py)). 
    Very responsible... ☹️ So I ran that script, and the endpoints did produce an error: "bike is bound." 
    No error when I use these endpoints with the account that owns the bike. Hooray, basic security. I'm happy.

**HOWEVER**... and that is a big however. There is one last way to get the encryption key: `getBikeByBleMac`. 
    It would seem this is the endpoint that is used for adding bikes to an account in the first place. 
    This endpoint failed validation. 
    There is no check on this particular endpoint to validate that the account that owns the token also owns the bike being requested, 
        as is done with the other endpoints.

Now... if there is an exposed endpoint, and AI won't write the script to abuse it... I know how I write my own code, I will!! 
    So I [did](https://github.com/rheard/heybike/blob/main/research/scripts/heybike_acl_check2.py) (more to come)... 
    In theory this could also be used to initiate the flawed firmware update process, which would require users to powercycle their bike, 
    which requires having the battery eject key, which I don't always ride with. 
    It was at this moment that I started to have FBI flashbacks... We'll come back to this.

## Firmware Update

First though, back to the original problem which started this project: I want to apply a firmware update to my bike.

First I tasked cortex with recreating the firmware downloading process, which it did without much issue (linked [here](https://github.com/rheard/heybike/blob/main/research/scripts/heybike_firmware_download.py)). 
    Basically you request the version information from your bike, send that to an API which will tell you if you need an update or not, and if you do, where to download it from. 
    The firmware updates are hosted on an Alibaba Cloud bucket, the same place I found the publicly indexed screenshot from the beginning of this blog post. 
    They are .vmfw files, seemingly encrypted.

Next I tasked cortex with recreating the firmware update process. 
    The firmware update process is initiated by a particular opcode which moves the bike into a YMODEM data transfer mode with 128-byte packets 
        to transfer the firmware file.

However I specifically told cortex: "the process in the app is bugged. If you just try to recreate that, it will fail. 
    You will need to add a lot of logging to figure out what is going wrong." 
    That is exactly what it did, it created a script to apply the vmfw files with a lot of print statements (linked [here](https://github.com/rheard/heybike/blob/main/research/scripts/heybike_firmware_update.py) after edits). 
    I ran the script, it started the update process and then froze, just like the app. 
    I started to analyze the logs when I thought "wait, why am I doing this" and I literally just dumped them into cortex. 
    It found exactly the problem. I ran again, my bike started updating, 20%, 35%, 60%, 80%, 95%, 110%, 135%... 
        it was at this moment I became alarmed and killed my script. It seemed to have got caught in an infinite loop. 
    I gave cortex the logs and it identified the exact same problem occurring at the end of the process, presumably from shared code which is bugged.

Essentially the issue is: after sending the opcode to initiate the firmware update process, the expected YMODEM startup sequence plays out. 
    For those who are naive (me), it looks like this:

    bike     -> C
    sender   -> YMODEM block 0 header
    bike     -> ACK
    bike     -> C
    sender   -> block 1 firmware data, etc...

However for some reason the bike does not follow the expected process; specifically it would never send the second C after the ACK. 
    This meant the bike was hanging around waiting for the firmware data to start coming in, while my script/the app was hanging around waiting for the second C to come from the bike before it started data transfer. 
    Similarly the YMODEM termination sequence similarly has the bike send an ACK followed by a C, and yet again, it would never send it. 
    The solution was to wait for the C with a timeout, and just start sending the data if it was never received (or just finish the termination process).

Amazingly though I didn't need to re-run because after killing the script, my bike rebooted with the new firmware version number! For the second time in 3 hours I was struck by how quickly I was moving. 
    Thanks to cortex, I was able to reverse engineer this company's app, identify and fix the issues, and successfully apply a firmware update when their own broken code could not. 
    The only reason it took this long was because I had to step away to find food!

## Responsible Disclosure

While I was having fun, as I said I was starting to have problematic flashbacks and was sweating. 
    After discussing it with friends, I decided to do the responsible thing and reach out to the company to engage in responsible disclosure. 
    The only contact information I could find was their customer service contact, the same one I ordered a replacement battery from. 
    Who knows, maybe they have a bug bounty program? Perhaps I could re-coup the cost of the expensive battery I just paid for?

So I emailed them, essentially loosely detailing that I may have found a minor security problem and I've also identified a bug in their firmware update process, 
    and is there a security or development contact in the company who might run a bug bounty program I could talk to? 
    They sent me a response asking for more details, so I identified the endpoint for them; I don't want to appear like I'm holding information hostage in exchange for something after all. 
    They sent me an obviously AI-generated response saying they appreciate my efforts and they have forwarded the information to their engineering team... 
    Roughly two weeks went by, I sent back a response basically asking if they have a timeline for remediation or if the company had a bug bounty process. 
    I received another obviously AI-generated email saying there was no bug bounty process and my notes have been forwarded. 
    I tried to look up engineering contacts for the company on LinkedIn, but I only found sales and marketing. 
    It is worth noting they have released a new version of the app which includes a new communication protocol for new bike models, as well as LED panel support... 
        but no firmware update fix or API lockdown.

Well... I feel I've done the responsible thing. While I give the company an A on replacement parts, I'll give them a D on responding to bug/security notifications. 
    At least I didn't get another C&D. I'll wait 90 days, and then post about it... and this all started 90 days ago, here I am!

# Finale

In that 90 days I hastily wrote a Python package creatively called heybike ([source](https://github.com/rheard/heybike/tree/main)). 
    Just do `pip install heybike` or similar. Users can use this to control a Heybike, and also successfully apply firmware updates! 
    Notably the following code works to turn off any nearby bike with no validation if the nearby bikes you're trying to control are owned by you:

```python
    for bike in Heybike.nearby_bikes(email=..., password=...):
        bike.set_power(False)
```

Please note I can only guarantee this package for Heybikes I'm able to test with, which in this case is a Heybike Cityrun 1.0. The next steps for me would be to try to continue decrypting the firmware, however that would probably require cracking my bike open and soldering things... but I like my bike. I don't want to possibly break or damage it. I think I'll leave things here for now.  
  
This was still quite a fun project. There is rightly a lot of controversy surrounding AI, but I just wanted to share an instance where it unlocked a speed that provided even more joy. If you're still with me, hopefully you've found this process somewhat entertaining/enlightening...