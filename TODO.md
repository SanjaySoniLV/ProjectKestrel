# Project Kestrel To-Do and Changelog


TODO:
* Implement and test distributed DirectML and CoreML Builds following pipeline v2.0 upgrade [DONE]
* Implement advanced analysis settings mode: [DONE]
    - Move the following analysis-related settings settings to be near the analyze folders dialog box.
        * Animal Detection Confidence Threshold: Default = 0.25
        * Max Birds Detected Per Image: Default = 10 + remove warning about memory overhead
        * Exposure Compensation Quality: Default = Balanced
        * Scene Grouping Time: Default = 1 second
    - Add a new setting "Parallel Processing" that specifies how many images to read to sustain the queue at once (Default = 3) with note to lower if your computer slows down too much and a maximum of 5. 
* Add settings to control thumbnail behavior. specifically, whether it is also exposure-corrected before being saved. this may require some pipeline reworking to persist the thumbnail data in memory, which can be expensive so make it false by default until we benchmark performance changes. [DONE]
* Add a check box near "group by folder" that controls whether each scene card is expanded to also show the square bird thumbnail of the image next to it. [DONE]
* Scrutinize settings schema validation and reproduce error with overwriting previous settings from earlier versions. Enforce some kind of never-overwrite-always-upgrade logic and tolerate additional fields being in the kestrel settings file. [DONE]
    > Related "* Fix "Dropped unsupported keys (1): culling_tutorial_seen\n127.0.0.1 - - [06/Apr/2026 13:13:59] \"GET / HTTP/1.1\" 200 -\n127.0.0.1 - - [06/Apr/2026 13:13:59]" tutorial keeps showing up."
* Add "Mark as Reviewed" button next to the species/family labels that marks those labels as "Reviewed". Right now it is only considered reviewed if the user interacts with those labels by deleting/re-adding some; there's no way to mark something that kestrel got right as 'reviewed' [DONE]
* Review speciesnet model; look at NO CV RESULT and change it to not be ignored. also print the reasons that any particuar bounding box is ignored; there are some images that say there are 1-3 animals above confidence threshold but none of them are evaluated any further. [DONE]
* Scrutinize database overwrites that are potentially leading to inconsistent behavior while analysis is still in progress getting rid of manually inputted data [DONE]
* Upgrade to enable experimental wildlife detection by default + Test [DEFERRED]
* Remove additional model weights from Git repo + verify LFS assets [DONE]
* Add ability to change which XML fields are written (ex. exclude ratings, labels). [DONE]
* Improve exposure compensation algorithm to fix dark birds against bright backlit sky edge case. [DONE]
* Remove "Unknown" from becoming a dedicated species label. [DONE]
* Website: Add sign-up list for cloud analysis or beta testing.
* Improve 'show bird thumbnail' behavior. [DONE]
* Fix bug where the image is like half-split for some reason. [DONE]
* Improve UI: Add "Open in [photo editor]" button; along with buttons to switch between the multiple bird crops. Near the right side. [DONE]
* Improve timeline: more granular detail than just grouping by capture hour. Also maybe add a "Adjust Capture Time" button that lets you add some hour offsets/change the capture time to sync it better. [DONE]
* Upgrade terms of service version retrieval to prompt updated terms warnings + Clarify file and folder names may be sent with crash reports that are automatic and non-opt-out. [DONE]
* Upgrade Kestrel Telemetry/Required Usage Data Collection to fetch the data from the website if appropriate. [DONE]
* Upgrade tutorial workflow: Simple and advanced version. Potentially also inform the user how they can integrate Kestrel into their workflows. [DONE]
* Add thumbnail quality settings for storage control. [DONE]
* Update ONNXRuntime-DirectML for windows in CI workflow.

PRE-LAUNCH:
- Update website, version.json, and internal "What's New" to reflect latest release notes.




TESTING:
* Continue to test pipeline v2 and performance with wildlife detections enabled. - check detection sensitivity thresholds, exposure compensation performance, and overall performance.
* Test whether recent metering/camera-white-balance changes have fixed reported errors from Reddit users.

DEFERRED TODO:
* Implement a fully optional login-based system where we can share an 'outing summary' that gets uploaded to projectkestrel.org (this is a point of sharing the platform with others!) - Kestrel Perch
    - will need to clarify terms and privacy policy here
    - will need to make sure user data isn't scraped/used in search engines/etc... i dunno. 
* Re-train quality classifier model on latest pipeline.
* Consider oppotunities to defer modes to filtering options, so re-analysis is not required.
* Investigate Sony HEIF image support
* Implement Kestrel Quick Export system (see below)


Features under consideration
* Quick export/Kestrel quick edit feature --> meant to be an intermediary between using a full-feature photo editor like lightroom/darktable and just getting the photo out there as quick as possible. 
    With the following features:
        - Crop
        - Denoise
        - Exposure adjustment
        - Shadows/Highlights adjustment
        - Color saturation adjustment
        - White balance adjustment
        - Pre-sets you can utilize (so you just tweak exposure, determine the crop)
        - maybe with AI denoise???
    Should take as little time as possible to export; then directly copies the exported photo into your clipboard and also features a way to access it again later.
* eBird integration support via user-provided API keys. [potentially pushed for next release] | STALLED due to no way to use api key to retrieve one's own checklists.



===================================================================================================================

# Version Yellow Warbler Changelog
* Massive improvement to Kestrel group detection methodology particularly for birds in flight
* Removed tendency to identify other non-bird animals and placed behind a dedicated checkbox
* Hidden "Use GPU When Available" if app is frozen due to lack of implementation in current system
* Add check for analysis version, prompt user whether they want to re-analyze an already analyzed folder that is on a lower version. I.e. italicize if version is lower.
* Add "Clear Kestrel Analysis" button to a right-click menu. 
* Add un-group scene dialog box to the scene view.
* Add "Save changes before opening Culling Assistant" check and verify "Save changes before exiting" check
* Fix UI for scene editing view.
* Fixed UI for settings page and expanded number of potential editors to several new options with a dedicated "Custom application" page.
* Fix UI - Live Analysis Page
* Added "Accept All" and "Reject All" buttons in changelog


# Version Swamp Sparrow Changelog
* Refactor code to make it easier to edit.
* Investigate poor performance in poorly-lit circumstances, even if it is just to add an up to 1-2 stop exposure adjust.? - For this we need to finish Kestrel Workshop. 
* Improve star rating system - this sort of punishes people with different equipment by setting all their photos to "1 star" and thus making the system pretty bad. Add a normalization option in settings that essentially fits the ratings distribution folder-wide to a uniform distribution with 20% splits. this would make sure the star ratings cover the entire breadth of the folder and propbably improve culling performance too ? Default = within folder normalization
* test new exposure correction algorithm
* implemented database correction
* Consider making the auto-grouping threshold an adjustable analysis setting and storing timestamp metadata for future use in a timeline view. And consider changing scene naming (from #123) to reflect timestamp of the first img in the scene and then you can group it by hour? That'd be sick. Let's do that as a much more intuitive main interface. Will need a database upgrade though.
* Fix scene tags issue
* Setting to control false positivity rate.
* RAW preview within visualizer


# Version Lincoln Sparrow Changelog
* Major update: Substantial improvements to quality estimation using a new machine learning model.
    - New exposure compensation algorithm applies exposure compensation to improve quality estimation performance in bright and dim images.
    - New machine learning model reflects these changes in the quality determination pipeline.
* New rating normalization algorithms let you control Kestrel's auto-determined ratings. Look for these options under "Settings" 
* Significant improvements to group detection methodology should reduce the number of false groupings.
* Several bug fixes and UI improvements
    - Fixed bugs with RAW preview handling on MacOS devices being blurry
    - Fixed bugs with inconsistent application of exposure correction algorithm
    - Fixed bugs preventing the user interface from updating to reflect newly analyzed images while analysis is in progress.
    - Fixed bugs in user interface related to the new split scene and scene tag modification system
    - Fixed a bug where the ETA calculation would not update when resuming analysis of a folder that has previously been started
    - Improved UI by reorganizing settings menu and providing several options to customize analysis parameters.
    - Improved UI to show max star rating rather than max Quality
    - Improved UI to implement auto-save functionality by default.
    - Improved UI to offer more information when a new version or update is available.

    
* ETA calculation fails when resuming a folder that started to be analyzed.
* Massive issues with exposure correction --> Definitely needs to target a higher overall EV and needs to apply to all images for quality esitmation to work properly. Currently any dark photo gets heavily penalized once exposure correct shifts it down.
* Exposure normalization should be global across all images - remove the minimum shift cap. 
* Ratings are showing as 1 star by default for every single photo until a bit of database backlog happens. Fix this. 
* Exposure normalization step isn't really working right. Should target a histogram or make it histogram based (see kingbird photos, etc)
* RAW preview seems downscaled in culling assistant clearly - something is broken there.
* Some issues with underexposed birds being overcorrected too... Maybe just fix this exposure correction algorithm to just shift the extreme cases?
    Bad examples:
        005, 006 in high island 2024 should not be such high quality... ?
* Auto updating folders and that behavior has been fixed. 
* May want to consider tightening mask probability threshold in mask-rcnn ?
* Split scene issue --> Doesn't exactly save automatically. The save changes feature isn't exactly working too well. I think we should just make it auto-save all changes and just maintain the revert changes button. 
* Improve culling.html so that default behavior on unrated scenes is to reject with user-customizable option within the culling options. 
* refresh behavior keeps refreshing when paused.
* Some group detection failures in low-feature-point space. (ex. scene #30 high island 2024) - fixed


# Version Willow Ptarmigan Changelog
* Major improvements to Kestrel User Interface! Kestrel now shows your scenes in a filmstrip style view, allowing you to rapidly relive your memories and select which ones to edit and share.
    - New keyboard shortcuts let you rapidly flick through a scene and seamless advance to previous/next scenes.
    - New accept/reject tagging system lets you make culling decisions from the scene visualizer
    - Streamlined user interface maintains all functionality: rename the scene, edit tags, split the scene into multiple scenes, and view RAW previews.
* Improvements to Kestrel Culling Assistant and handling of culling decisions
    - New streamlined user interface allows you to drag and drop images in addition to using Shift+Click
    - New buttons to reset Culling Decisions allow you to reset Accept/Reject Ratings
* Significant improvements to Kestrel's analysis pipeline
    - New configurable rating system lets you customize how Kestrel assigns star ratings to your scenes.
    - New exposure compensation algorithm improves analysis options
    - Fixed bugs with Kestrel's scene grouping algorithm.
* Other User Interface tweaks
    - Consolidated buttons in the main GUI
    - Updated in-app tutorials to thoroughly explain all new features and workflows
    - Improvements to how Kestrel writes metadata to align more consistently with visuals.
    - Tweaks to simplify language around metadata and culling categories.
    - Improved handling of auto-generated ratings and auto-generated culling decisions to enhance consistency and decouple the two features.
    - Show pipeline version in addition to standard version control.
* Substantial number of bug fixes, particularly around the user interface, settings menu, and culling assistant.


# Version Kentucky Warbler Changelog
Major update featuring significant corrections to exposure compensation algorithm, improved quality scoring model, many UI improvements, bug fixes, compatibility improvements, security improvements, and more.

## Major Changes
* New Exposure compensation solver algorithm ensures that the bird in the image is always properly exposed. This improves quality ranking accuracy, species detection accuracy, and makes it easier to review your photos!
    * New setting to tweak exposure compensation solver performance to your needs.
* Substantial user experience improvements
    * New "Quick Copy" buttons let you directly copy bird crops or whole image exports to your clipboard for instant sharing without even going through an editor.
    * New adjustable divider lets you resize the bird crop to full image preview space.
    * New "Suggested Species" tags make it easier to add tags based on machine learning model outputs.
    * Improved split scene behavior to make it substantially more intuitive (use Shift+Click to split scenes)
    * "Only manually reviewed scenes" toggle now reflects culling decisions, scenes with reviewed tags, and renamed scenes in addition to manually applied star ratings.
    * Fixed CTRL/CMD Keyboard shortcuts for MacOS systems
* Substantial improvements to Kestrel's handling of multiple birds within the same image.
    * Kestrel will now detect, analyze, and save results of multiple birds within the same image (up to 5 by default, with user customizable settings)
    * Browse through all of the birds that Kestrel detects using the up/down arrow keys! Hit "Enter" to assign a specific bird as primary (default: highest-quality bird). 
* Upgraded quality classification model to reflect the latest pipeline. Quality ranking performance is substantially improved based on current tests.
* Improved performance on scenes with a large amount of subjects via backend machine learning model parameter tweaks.
* Made several changes to improve security (reduced sensitive HTTP endpoint exposure, enforced settings schema validation, etc.)
* Improved crash handling/crash recovery system, with automatic restoration of the queue.

## Minor Changes
* Fixed bugs on Canon EOS R5 Mk II by upgrading rawpy dependency
* Upgraded tensorflow, torch, and other dependencies to latest versions
* Improved auto-save performance and consistency across all changes
* Fixed bugs related to loading/unloading folders while analysis is in progress.
* Fixed a bug where JPG files would not be co-moved via the culling assistant.
* Fixed bugs with scene navigation.

## Known Issues
* With the implementation of the new exposure compensation algorithm, analysis time may be slightly slower. Set the exposure compensation profile to "Normal" or "Lenient" to reduce this overhead.
* Default star rating normalization algorithm may be somewhat sub-optimal; improvements are planned but will require some thinking.

## Coming soon
* Website will be updated to reflect latest analysis framework and new features.


=== archive===


Bugs
* Known issue that skipping to next scene works inconsistently, and the background view should scroll to match the current scene opened in the film strip. [DONE]
* Add "Parent folder" button to analyze folders directory so you can walk up the folder tree if needed. 
* Fix GPU non-supported in Github documentation. [DONE]
* RAW+JPG co-movement problem in Culling Assistant. [DONE]
* Frequent failures when running on images with many subjects; find a way to lower Mask-RCNN region proposal threshold to improve performance. [DONE]
* Known issue where exposure compensation still tends to be slightly too dark, particularly for noisy images. Root cause unknown. [DONE]
* Auto-save may save all folders rather than just the ones that have been modified. [DONE]
* Bug where un-loading a folder that has recently been analyzed sometimes causes it to auto-check itself. Uncertain why. In this situation, hitting the next arrow to skip to the next scene sometimes goes to the next scene of a different folder, if it is loaded with a similar capture time. We need to make sure that the next scene is consistent with the way that the scenes are displayed in the main view. 
* PIPELINE CHANGE New ML model is needed to correctly handle the exposure shift adjustements. [TEMP-DONE]
* Delete cache files after folder unloaded or software closed. [DONE]
* Fix bad UI problem when splitting a scene. [DONE]
* Tweak exposure compensation algorithm to be slightly more aggressive in correcting exposure changes; making sure that highlights are not blown out while also making sure that shadows or highly underexposed birds are properly adjusted and lifted. Potentially exclude some of the boundary pixel layers during exposure compensation calculation as they my reflect mask-rcnn boundary inconsistencies. [DONE]
* Restore Queue bugs - It doesn't seem to be working. The intention is if the program crashes mid-analysis, Kestrel can detect that something went wrong. It will then 1) say "I crashed. want me to send crash report?" 2) "clean crash log" [DONE]
* No clean crash log; all current print statements go to terminal output. Need a cleaner way to pipe these into a userprofile's kestrel logs folder. Also too many terminal outputs; some of these need to be cleared to the extent possible. [DONE]
--> Remove all "read_image_for_pipeline" logs
--> remove telemetry debug calls 
* Canon EOS R5 Mark II RAW Decode Issue --> Bumped Rawpy dependency from 0.23.1 to 0.26.1 [DONE]
* Resolve dependabot dependency security vulnerability alerts [DONE]
* Conduct comprehensive security review. [DONE]
* Security Fixes: remove browser mode fallback, tighten exposed API endpoints, enforce schema validation for settings files, tighten path normalization escapes, and reduce blast radius of auth token breach. [DONE]
* Some exposure compensation adjustment failures for overexposed images persists [DONE]
* Swapped position of the bird crop and full image thumbnail. [DONE]
* Added adjustable divider to control size of the two UI elements. [DONE]


Features under consideration
* Add a "Quick Export" system or copy thumbnail system. [DONE]
* Implement "Suggested" system to manually reclassify species quickly based on majority confidence-weighted vote of all scene components. [DONE]
* Multi-subject mode is not handled super consistently. Consider reworking pipeline to store crop exports of all detected subjects for improved analysis. [DONE]
* Implement "Analyze JPGs instead of RAWs" with clear warning that analysis on RAWs is strongly preferred since JPG compression artifacts can dramatically alter quality scores. [DEFERRED]
* Investigate GPU support from recent pull request #14
* Alter search "show only manually reviewed photos" to include those with manual culling decisions or species selections. [DONE]
* Add restore capability that persists after closing/reopening Culling Assistant/Kestrel [DONE]
* eBird integration: After integrating Clerk (log-in system), add ability to connect eBird account. Then add ability to attach a particular checklist to an outing. Add ability to hit "T" to enter tagging mode, and add capability to turn the text box into a text+combobox which fetches from the current checklist. Then, add ability to upload photos to eBird/Malaculay library from in-app. [DEFERRED]

Test before release:
* Fix to Mask-RCNN Region Proposal System [DONE]
* Quality classifier performance on revised pipeline [DONE - WORKS WELL ENOUGH]
* RAW+JPG co-movement fix in Culing Assistant + test restore capability persists after Kestrel re-opens. [DONE, FIXED]
* Improvement to crash handling and error logging. [DONE]
* Test rawpy decode issue. [DONE, FIXED]
* Test Queue restore fixes [DONE]


# Version Gambel's Quail Changelog
Most significant update to date: Kestrel is up to 500% faster while achieving better analysis quality! Kestrel also supports more than 1200 wildlife species!

## Key Changes
* New pipeline version 2.0 enables up to 500% performance improvement over v(Kentucky Warbler) while *improving* bird detection sensitivity and exposure compensation performance.
    - New wildlife detection model powered by MegaDetector (github:Microsoft/CameraTraps) leads to significant improvements in performance and animal detection sensitivity.
    - New wildlife species classification model powered by SpeciesNet (github:Google/SpeciesNet) enables identification of up to 1200 wildlife species!
    - New image segmentation model (github:SysCV/SAM-HQ) leads to substantial improvements in identifying specific bird pixels. 
    - Migration of all of these models, along with existing quality classifier to ONNXRuntime allows GPU utilization, leading to substantial performance improvements on all systems.
    - New exposure compensation algorithm achieves significantly better performance on poorly-lit bird images while performing 10x faster than v(Kentucky Warbler).
    - New analysis setting allows parallel processing of multiple files simultaneously, leading to up to 3x performance improvement in RAW decode speed.
    - Together, this new pipeline is FASTER *and* MORE ACCURATE! THANK YOU to the wonderful teams behind SpeciesNet, MegaDetector, and SAM-HQ for their amazing open-source work!
* Large number of user interface tweaks makes Kestrel more intuitive:
    - All analysis-specific settings are moved to the Analyze Folders Dialog box for clarity and proximity.
    - Improved timeline visualization no longer strictly groups photos by the hour. Additionally, new "Adjust Capture Time" dialog box allows you to correct your camera's capture time (ex. Daylight Savings Time, etc.)
    - Exposure compensation is now applied to the image thumbnails (toggle-able in settings), enabling better viewing of image details across lighting conditions.
    - New ability to see the bird thumbnail while browsing your photos.
    - Improvements to Scene view UI: "Mark as Reviewed" button, "Open in [photo editor]" button, and buttons to switch subjects in multi-subject mode.
    - Revamped/Improved in-app tutorials provide better guidance with "Basic" and "Advanced" mode.

## Minor Changes
* Updated Privacy Policy and Terms of Use to clarify that some file names may be sent when reporting bugs or crashes. 
* Several bug fixes
    - Fixed bugs where the tutorial would keep popping up for some users who upgraded their version of Kestrel
    - Fixed bugs where Kestrel would erroneously believe that it crashed, prompting the user to ask whether to send a crash report.
    - Fixed bugs where images would appear 'split in half' when viewed rapidly.
    - Fixed a bug causing user-specified culling decisions and similar edits to be overwritten if Kestrel was analyzing the folder while the user was reviewing their photos.
    - Fixed bugs causing the image to appear hazy and tinted for certain scenes and camera models.
