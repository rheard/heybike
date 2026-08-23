# Heybike App API Endpoint Inventory

This documents the app-declared first-party HTTP surface found in the decompiled sources. Third-party SDK and China-linked data flows, including Aliyun/Alibaba/Taobao and TalkingData endpoints, are summarized below and analyzed in `DATA_COLLECTION_AND_CHINA_ENDPOINTS.md`.

## Sources Used

- Retrofit API interface: `sources/com/yulai/heybike/net/b.java`
- API wrapper/repository: `sources/com/yulai/heybike/net/a.java`
- Retrofit/OkHttp factory: `sources/com/yulai/heybike/net/d.java`
- Base URL constants: `sources/em/g.java` and `sources/com/yulai/heybike/ui/a.java`
- Response converter/status handling: `sources/rm/c.java`, `sources/rm/b.java`, `sources/com/yulai/uilib/net/data/NetBase.java`
- Token persistence: `sources/com/yulai/heybike/manager/h.java`, `sources/com/yulai/heybike/data/type/UserToken.java`, `sources/com/yulai/heybike/data/type/RespRegister.java`

## Transport

- API base URL: `https://heyapi.heybike.com/`
- Retrofit is created with that base URL and `com.yulai.heybike.net.b` as the API interface.
- Timeouts: connect `10s`, read `30s`, write `30s`.
- Converters: custom response converter first, then Gson.
- Logging: `HttpLoggingInterceptor.Level.BODY` is enabled in the decompiled build, and another interceptor logs form parameters and response codes.

## Common Headers

The OkHttp interceptor adds these headers to every Retrofit request:

| Header | Value source |
|---|---|
| `systemtype` | constant `android` |
| `phoneInfo` | app version string, observed as `v4.6.0` |
| `phoneType` | manufacturer/brand/model/CPU ABI string |
| `phoneSystems` | Android OS/API level string |
| `source` | constant `1` |
| `language` | app/system language key |
| `countryCode` | `Locale.getDefault().getCountry()` |

## Auth Model

Authentication is not sent as an `Authorization` header. The app passes a user token in a request parameter named `token`.

- Login/register-style endpoints return `RespRegister`, which contains `token`, `pushId`, and `smsCode`.
- The persisted user credential is `UserToken { token, email }` stored in shared preferences under `UserData` key `sp_user_token_new`.
- `ApiRepository.n0()` returns `manager.h.n().getToken()` and wrapper methods pass it as either a form field named `token` or a JSON body field named `token`.
- The custom response converter treats `status == 200` as success.
- `status == 205` is treated as token invalid and triggers the app's token-invalid path.
- Other non-200 statuses throw an API error carrying the parsed response object.

All typed responses extend or behave like `NetBase { status, message }`; the output model column below names the additional typed payload class declared by Retrofit. `NetBase` means the app declared no additional response payload type.

## Notable Bike/Account Endpoints

- `appHeyApi/getBikeGpsIMEI`: fetches GPS history by IMEI; lost-mode code calls it with `num = "100"`.
- `appHeyApi/setDefultBike`: misspelled server route that persists many bike settings, including anti-theft/fence flags, auto-lock, backlight, gear, max speed, start gear, speed unit, BLE link/apply state, ride mode, ride feel, and speed-independent limiter mode.
- `appHeyApi/openCloseBike`: server-side 4G open/close operation keyed by `deIMEI` and `openType`; separate from BLE power control.
- `appHeyApi/getBikeBleKey` and `appHeyApi/getBikeByBleMac`: retrieve BLE binding/key material.
- `appHeyApi/getBikeIMEIUpload` and `appHeyApi/updateBikeVersion`: OTA metadata and version reporting.
- `appHeyApi/getErrorCodes` and `appBikeApi/saveHardwareErrorLog`: bike fault-code lookup/reporting path.
- `appHeyApi/loginThird`: third-party login; the Google flow sends Google account ID/email with `thirdType = 0`, not a Google ID token.

## Authorization Review

See `ENDPOINT_AUTHORIZATION_REVIEW.md` for the IDOR-focused pass over client-supplied identifiers such as `deBle`, `deIMEI`, `userId`, `mId`, `useId`, content IDs, and comment IDs. That file separates authentication from object authorization: most endpoints include a `token`, but many also rely on server-side checks to prove the requested bike, user, ride log, membership, or content object is actually accessible to that token.

## Endpoint Table
| Source line | Method | Path | Inputs | Output model | Auth |
|---:|---|---|---|---|---|
| 186 | POST | `appHeyApi/getMailPushs` | form: token | `RespMessage` | token field |
| 190 | POST | `appBikeApi/topic/detail` | body: TopicBody {token, topicId} | `TopicDetailInfo` | body token |
| 194 | POST | `appBikeApi/ridingMoments/banner` | body: TokenInfo {token} | `BannerInfo` | body token |
| 199 | POST | `appHeyApi/otaMonitor/report` | form: token, bikeTypeId, successRate | `NetBase` | token field |
| 203 | POST | `appBikeApi/ridingMoments/collect` | body: OperationBody {id, status, token, userId} | `NetBase` | body token |
| 208 | POST | `appHeyApi/userFeedback` | form: token, deIMEI, userName, bikeType, buyOrder, feedInfo, userEmail, feedPic, feedType, feedId | `NetBase` | token field |
| 212 | POST | `appBikeApi/ridingMoments/block` | body: BlockBody {status, token, userId} | `NetBase` | body token |
| 217 | POST | `appHeyApi/doUploadBike` | form: token, deIMEI, openType, failType, failReasopn | `NetBase` | token field |
| 227 | POST | `appHeyApi/deleteAddInfo` | form: token, addId, isdel | `NetBase` | token field |
| 232 | POST | `appHeyApi/addUserBikeByBle` | form: token, deIMEI, deMac, iotType, deType, remark, isOften, deICCID | `NetBase` | token field |
| 236 | POST | `appBikeApi/ridingMoments/tipOff` | body: ReportBody {content, id, token, type} | `NetBase` | body token |
| 241 | POST | `appHeyApi/setStoreInfo` | form: token, shopId, isdel | `NetBase` | token field |
| 245 | POST | `appBikeApi/ridingMoments/list` | body: MomentsBody {pageNum, pageSize, query, token} | `MomentsInfo` | body token |
| 250 | POST | `appHeyApi/updateUserInfo` | form: token, userPhone, userName, userImg, userWeight, userHeight, signature, heightUnit, language, timezoneId, countryId, provinceId, cityId, birthday | `NetBase` | token field |
| 255 | POST | `appHeyApi/addUserBike` | form: token, deIMEI, deMac, isOften | `NetBase` | token field |
| 259 | POST | `appBikeApi/getFiveYearCouponList` | body: CommonListBody {pageNum, pageSize, token} | `FiveYearCouponInfo` | body token |
| 264 | POST | `appHeyApi/getBikeBatInfo` | form: token, deIMEI | `NetBase` | token field |
| 269 | POST | `appHeyApi/buySim` | form: token, deIMEI, simCode | `NetBase` | token field |
| 273 | POST | `appBikeApi/activity/popularityValueList` | body: CommonListBody {pageNum, pageSize, token} | `PopularityInfo` | body token |
| 277 | POST | `appBikeApi/ridingMoments/newMessageRemind` | body: TokenInfo {token} | `MessageInfo` | body token |
| 281 | POST | `appBikeApi/banner/activity` | body: TokenInfo {token} | `BannerInfo` | body token |
| 285 | POST | `appBikeApi/trackingPoint` | body: PointBody {deviceId, elementId, elementName, eventId, extra, messageId, pageName, token} | `NetBase` | body token |
| 290 | POST | `appHeyApi/findStoreByName` | form: token, yuName | `RespSearchStore` | token field |
| 295 | POST | `appHeyApi/getUser4GBikes` | form: token | `UserBindBikes` | token field |
| 299 | GET | `appHeyApi/getLanguge` | - | `LanguageKeyBean` | none |
| 303 | POST | `appBikeApi/baseUserInfo` | body: TokenInfo {token} | `BaseUserInfo` | body token |
| 307 | POST | `appBikeApi/topic/publishList` | body: TokenInfo {token} | `PublishTopicInfo` | body token |
| 312 | POST | `appHeyApi/getUserUseLog` | form: token, deIMEI, num, year, month | `RideHistory` | token field |
| 317 | POST | `appHeyApi/getMailPushCount` | form: token, pushType | `RespPushCount` | token field |
| 321 | POST | `appBikeApi/urgentFeedback` | body: UrgentFeedback {bleKey, deBle, deIMEI, errorInfo, token, type} | `NetBase` | body token |
| 326 | POST | `appHeyApi/openCloseBike` | form: token, deIMEI, openType | `NetBase` | token field |
| 331 | POST | `appHeyApi/addBikeModeByIMEI` | form: token, deIMEI, bikeGear, bikeSpeed, bikeCur | `NetBase` | token field |
| 336 | POST | `appHeyApi/getAppVersion` | form: token, phoneType | `AppUpgradeInfo` | token field |
| 340 | POST | `appBikeApi/setBikeBaseInfo` | body: MileageInfo {deIMEI, mileage, token} | `BikeBaseInfo` | body token |
| 345 | POST | `appHeyApi/getPushInfoCount` | form: token, pushType | `RespPushCount` | token field |
| 350 | POST | `appHeyApi/getWeekData` | form: token | `RespWeekData` | token field |
| 354 | POST | `appBikeApi/userCenter/detail` | body: TokenUserBody {token, userId} | `UserCenterInfo` | body token |
| 358 | POST | `appBikeApi/ridingMoments/list` | body: TopicMomentsBody {pageNum, pageSize, query, token, topicId} | `MomentsInfo` | body token |
| 362 | POST | `appBikeApi/ridingMoments/userStatus` | body: TokenInfo {token} | `UserStatusInfo` | body token |
| 367 | POST | `appHeyApi/getUserFeedLogs` | form: token, deIMEI, feedId, feedStatus | `NetBase` | token field |
| 371 | POST | `appBikeApi/saveHardwareErrorLog` | body: ErrorFeedback {deIMEI, errorCode, token} | `NetBase` | body token |
| 376 | POST | `appHeyApi/removeUser` | form: token | `NetBase` | token field |
| 381 | POST | `appHeyApi/getBikeByBleMac` | form: token, deBle | `BikeBlekeyInfo` | token field |
| 386 | POST | `appHeyApi/uploadByFtp` | form: token, deIMEI, openType, ftpUrl | `NetBase` | token field |
| 390 | POST | `appHeyApi/uploadLog` | multipart body from wrapper: token, logType=1, phoneInfo, file=log.txt | `NetBase` | body token |
| 394 | POST | `appBikeApi/ridingMoments/publish` | body: PublishInfo {content, imageList, token, topicId} | `NetBase` | body token |
| 398 | POST | `appHeyApi/uploadFile` | multipart: file, filename=heybike.jpg: RequestBody | `UploadFile` | none |
| 404 | POST | `appHeyApi/getBikeByIMEI` | form: token, deIMEI | `BikeInfo` | token field |
| 408 | POST | `appBikeApi/reportData` | body: ReportInfo {bannerId, token, type} | `NetBase` | body token |
| 412 | POST | `appBikeApi/getRank` | body: TokenInfo {token} | `RideRank` | body token |
| 417 | POST | `appHeyApi/loginThird` | form: thirdID, thirdType, phoneInfo, phoneType, phoneSystems, userEmail, emailType, emailCode, userPass | `RespRegister` | none |
| 422 | POST | `appHeyApi/resetPass` | form: userEmail, emailCode, phoneInfo, phoneType, phoneSystems, userPass | `NetBase` | none |
| 427 | POST | `appHeyApi/changeAdmin` | form: token, mId, deIMEI | `NetBase` | token field |
| 432 | POST | `appHeyApi/getPushInfos` | form: token | `RespMessage` | token field |
| 437 | POST | `appHeyApi/getBikeModeByIMEI` | form: token, deIMEI | `BikeModeInfo` | token field |
| 442 | POST | `appHeyApi/login` | form: userEmail, phoneInfo, phoneType, phoneSystems, userPass | `RespRegister` | none |
| 446 | POST | `appBikeApi/ridingMoments/blockList` | body: CommonListBody {pageNum, pageSize, token} | `BlockInfo` | body token |
| 451 | POST | `appHeyApi/getUserAddInfo` | form: token | `RespCollectAddress` | token field |
| 455 | POST | `appBikeApi/ridingMoments/follow` | body: OperationBody {id, status, token, userId} | `NetBase` | body token |
| 459 | POST | `appBikeApi/comment/like` | body: CommentLikeBody {commentId, status, token} | `NetBase` | body token |
| 464 | POST | `appHeyApi/getUserBikes` | form: token | `UserBindBikes` | token field |
| 468 | POST | `appBikeApi/wearMedal` | body: WearMedalInfo {medalId, status, token} | `NetBase` | body token |
| 472 | POST | `appBikeApi/ridingMoments/delete` | body: TokenBody {id, token} | `NetBase` | body token |
| 477 | POST | `appHeyApi/updateUserInfo` | form: token, language, timezoneId | `NetBase` | token field |
| 482 | POST | `appHeyApi/removeUserBike` | form: token, deIMEI | `NetBase` | token field |
| 487 | POST | `appHeyApi/getUseCount` | form: token, deIMEI | `UserCountInfo` | token field |
| 492 | POST | `appHeyApi/setMailPush` | form: token, pushId, type, tableType, pushType | `NetBase` | token field |
| 496 | POST | `appBikeApi/userCenter/followList` | body: FollowListBody {pageNum, pageSize, token, type} | `FollowInfo` | body token |
| 501 | POST | `appHeyApi/sendEmailCode` | form: userEmail, sign | `RespSendSMS` | none |
| 506 | POST | `appHeyApi/getInstructionList` | form: token | `ManualInfo` | token field |
| 510 | POST | `appBikeApi/ridingMoments/messageList` | body: CommonListBody {pageNum, pageSize, token} | `MomentsMsgInfo` | body token |
| 515 | POST | `appHeyApi/getFeedBackInfo` | form: token, id | `RespFeedbackDetail` | token field |
| 519 | POST | `appBikeApi/comment/list` | body: CommentListBody {contentId, pageNum, pageSize, token} | `CommentListInfo` | body token |
| 524 | POST | `appHeyApi/getBikeGpsIMEI` | form: token, deIMEI, num | `BikeGpsBean` | token field |
| 529 | POST | `appHeyApi/getFeedBacks` | form: token, feedStatus | `RespFeedback` | token field |
| 534 | POST | `appHeyApi/userRegister` | form: userEmail, emailCode, userPhone, phoneInfo, phoneType, phoneSystems, userPass | `RespRegister` | none |
| 539 | POST | `appHeyApi/getUserInfoByToken` | form: token | `UserInfo` | token field |
| 544 | POST | `appHeyApi/getBikeBleKey` | form: token | `BikeBlekeyInfo` | token field |
| 548 | POST | `appBikeApi/ridingMoments/like` | body: LikeInfo {id, status, token} | `NetBase` | body token |
| 552 | POST | `appBikeApi/userInfo` | body: TokenInfo {token} | `UserPersonalInfo` | body token |
| 557 | POST | `appHeyApi/setDefultBike` | form: token, deIMEI, avoidTheft, nickName, fenceRange, autoOpen, isOften, autoLock, lockDis, isLinkSet, backLight, bikeGear, maxSpeed, startGear, batGrade, isBind, speedUnit, setByBle, setColor, handleGear, handlePWM, ridingMode, rideFeel, speedIndependent | `NetBase` | token field |
| 562 | POST | `appHeyApi/addBikeMemBer` | form: token, deIMEI, userEmail | `NetBase` | token field |
| 566 | POST | `appBikeApi/topic/list` | body: TokenInfo {token} | `TopicInfo` | body token |
| 571 | POST | `appHeyApi/removeBikeMemBer` | form: token, deIMEI, mId | `NetBase` | token field |
| 576 | POST | `appHeyApi/updateBikeInfo` | form: token, deIMEI, deICCID | `NetBase` | token field |
| 586 | POST | `appHeyApi/getErrorCodes` | form: token | `BikeErrorCode` | token field |
| 591 | POST | `appHeyApi/addUserUseLog` | form: token, deIMEI, startTime, endTime, cycMil, maxSpeed, avSpeed, useLength, gpsInfo, calorie, carbonReduction, saveTime, startEndAddress, ridingSeconds | `NetBase` | token field |
| 596 | POST | `appHeyApi/getBikeIMEIUpload` | form: token, deIMEI, hardVersion, oldVersion | `OTAUpgrade` | token field |
| 600 | POST | `appBikeApi/ridingMoments/deleteMessage` | body: TokenBody {id, token} | `NetBase` | body token |
| 605 | POST | `appHeyApi/updateBikeVersion` | form: token, deIMEI, iotVersion, iotHardVersion | `NetBase` | token field |
| 609 | POST | `appBikeApi/getUserMedals` | body: TokenInfo {token} | `UserMedalInfo` | body token |
| 613 | POST | `appBikeApi/saveUserCountryInfo` | body: CountryInfo {countryCode, token} | `NetBase` | body token |
| 618 | POST | `appHeyApi/deleteUserRideLog` | form: token, useId | `NetBase` | token field |
| 622 | POST | `appBikeApi/userCenter/list` | body: UserCenterBody {pageNum, pageSize, query, tab, token, userId} | `MomentsInfo` | body token |
| 627 | POST | `appHeyApi/getBikeSimInfo` | form: token, deIMEI | `BikeModelTypeList` | token field |
| 631 | POST | `appBikeApi/getFiveYearActivityInfo` | body: TokenInfo {token} | `FiveYearActivityInfo` | body token |
| 636 | POST | `appHeyApi/setAddInfo` | form: token, addId, addName, addDetail, addGps | `NetBase` | token field |
| 640 | POST | `appBikeApi/userMileageInfo` | body: TokenInfo {token} | `UserMileageInfo` | body token |
| 645 | POST | `appHeyApi/getNearStroe` | form: token, nearRadius, iotLo, iotLa | `RespNearStore` | token field |
| 649 | POST | `appBikeApi/comment/publish` | body: CommentPublishBody {commentContent, commentId, contentId, token} | `CommentPublishInfo` | body token |
| 654 | POST | `appHeyApi/getUserShopInfo` | form: token | `RespCollectedStore` | token field |
| 658 | POST | `appBikeApi/ridingMoments/detail` | body: TokenBody {id, token} | `MomentsDetailInfo` | body token |
| 581 | POST | `appHeyApi/getAllBikeType` | form: token | `BikeModelTypeList` | token field |
| 222 | POST | `appHeyApi/getAllBikeColorType` | form: token, deType | `BikeColorInfo` | token field |

## JSON Body Models

These are the request body models used by `@Body` endpoints. Decompiled Kotlin fields named like `f31647id` are shown as `id` where the decompiler comments indicate the original field was `id`.

| Body model | Fields |
|---|---|
| `TokenInfo` | token |
| `TopicBody` | token, topicId |
| `OperationBody` | id, status, token, userId |
| `BlockBody` | status, token, userId |
| `ReportBody` | content, id, token, type |
| `MomentsBody` | pageNum, pageSize, query, token |
| `CommonListBody` | pageNum, pageSize, token |
| `UrgentFeedback` | bleKey, deBle, deIMEI, errorInfo, token, type |
| `MileageInfo` | deIMEI, mileage, token |
| `TokenUserBody` | token, userId |
| `ErrorFeedback` | deIMEI, errorCode, token |
| `PublishInfo` | content, imageList, token, topicId |
| `ReportInfo` | bannerId, token, type |
| `TokenBody` | id, token |
| `CommentLikeBody` | commentId, status, token |
| `WearMedalInfo` | medalId, status, token |
| `FollowListBody` | pageNum, pageSize, token, type |
| `CommentListBody` | contentId, pageNum, pageSize, token |
| `LikeInfo` | id, status, token |
| `CountryInfo` | countryCode, token |
| `UserCenterBody` | pageNum, pageSize, query, tab, token, userId |
| `TopicMomentsBody` | pageNum, pageSize, query, token, topicId |
| `PointBody` | deviceId, elementId, elementName, eventId, extra, messageId, pageName, token |
| `CommentPublishBody` | commentContent, commentId, contentId, token |

## Hardcoded Web/Document URLs

These are not Retrofit endpoints, but the app constructs or opens them directly.

| URL | Use |
|---|---|
| `https://heyapi.heybike.com/appBikeApi/guide` | App guide WebView URL built from the API base URL |
| `https://heyapi.heybike.com/appBikeApi/4gPlan` | 4G plan WebView URL built from the API base URL |
| `https://heyapi.heybike.com/appBikeApi/ridingMoments/rule` | Riding Moments rule WebView URL |
| `https://heyapi.heybike.com/appBikeApi/fiveYearActivityRule` | Five-year activity rule WebView URL |
| `https://manage.heybike.com/heybike/vipTerms.html` | VIP terms |
| `https://manage.heybike.com/heybike/privacyPolicy.html` | Privacy policy |
| `https://manage.heybike.com/heybike/termsConditions.html` | Terms and conditions |
| `https://www.heybike.com/` | Public website |
| `https://heybike.zendesk.com/hc/en-us/categories/4404380549273-Product-Support` | Product support |
| `https://heybike.zendesk.com/hc/en-us/categories/4404397339417-General` | General support |
| `https://heybike.oss-us-west-1.aliyuncs.com/applicationGuide.pdf` | Application guide PDF |
| `https://docs.google.com/gview?url=<encoded-url>&embedded=true` | PDF viewer wrapper |
| `https://play.google.com/store/apps/details?id=com.yulai.heybike.new` | Play Store app page |

## Third-Party SDK and Runtime-Provided URLs

These are outside the first-party Retrofit interface, but they matter for endpoint coverage.

| Endpoint / Host | Source | Notes |
|---|---|---|
| `mpush-api.aliyun.com` | Aliyun Push SDK default host | App initializes Alibaba Cloud Push and binds `MD5(lowercase(email))` as an alias. |
| `https://mpush-api.aliyun.com/config` | Aliyun Push SDK config URL | Built from the default push host. |
| `msgacs.cn-zhangjiakou.aliyuncs.com` | Aliyun ACCS default app-connect host | China-region Aliyun hostname by name. |
| `jmacs.cn-zhangjiakou.aliyuncs.com` | Aliyun ACCS silent-connect host | China-region Aliyun hostname by name. |
| Aliyun push paths: `/active`, `/add-alias`, `/remove-alias`, `/push-status`, `/push-switch`, `/bind-tag`, `/unbind-tag`, `/list-tag`, `/list-alias`, `/bind-account`, `/unbind-account`, `/set-phone`, `/unset-phone` | Bundled Aliyun Push SDK | The app directly uses registration/add-alias/remove-alias; the rest are SDK capabilities. |
| `https://heybike.oss-us-west-1.aliyuncs.com/applicationGuide.pdf` | Hardcoded app guide URL | Also listed above as a directly opened document URL. |
| Server-returned `OTAUpgrade.IotInfo.otaUrl` / `ftpUrl` | `appHeyApi/getBikeIMEIUpload` response | Firmware download/reporting URLs are supplied at runtime and can be Aliyun OSS even when not hardcoded in the APK. |
| Server-returned `UploadFile.fileUrl` | `appHeyApi/uploadFile` response | Uploaded image/media URL is server-provided. |
| TalkingData/TendCloud hosts such as `api.talkingdata.com`, `www.talkingdata.net`, `tdsdk.cpatrk.net`, `cloud.cpatrk.net`, `me.cpatrk.net`, `tdsdk.xdrig.com` | Bundled analytics SDK | SDK is initialized, but the app config disables IMEI/MEID, MAC, app-list, and location collection. |

See `DATA_COLLECTION_AND_CHINA_ENDPOINTS.md` for the data fields, auth model, and confidence level for these SDK/runtime endpoints.

## Caveats

- Input names come from Retrofit annotations and decompiled request model fields. They describe what the app sends, not necessarily the full server-side schema.
- Some wrapper methods set defaults that are not visible in the raw interface table. Examples: `getBikeGpsIMEI` uses `num = "100"` in lost mode; `getAppVersion` defaults `phoneType = "2"`; `setDefultBike` sends blanks/defaults for fields it is not updating.
- `appHeyApi/uploadLog` is declared as a raw `MultipartBody`; the wrapper adds `token`, `logType = 1`, `phoneInfo`, and `file = log.txt`.
- `appHeyApi/uploadFile` is multipart image upload and does not include a token in its Retrofit declaration or wrapper call.
