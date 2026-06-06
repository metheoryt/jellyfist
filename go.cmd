# import everything
airdrome import .\data\apple\AppleMusicLibrary.xml
airdrome import '.\data\apple\Apple Media Services information Part 1 of 2.zip'
airdrome import .\data\listenbrainz\listenbrainz_metheoryt_1774472711.zip
airdrome import C:\Users\methe\Music\PicardedMusic\

# after all data is collected,
# match tracks between each other, create canonical tracks and playlists
airdrome resolve -t 0.4 --merge-playlists

# deduplicate
airdrome library import-duplicates  # load manual choices file (if left from previous runs)
airdrome library auto-deduplicate -s "artist,duration" -s "artist,year" -s "album_artist,duration" -c year
airdrome library deduplicate -c year  # manual step
airdrome library export-duplicates # save back to a file

# copy all files into configured directory. Main files separately, copies - separately.
airdrome library organize -c

# $$$
# After that, start fresh Navidrome pointing to the same directory.
# Let it scan it.
# Note the database (navidrome.db) path.
# $$$

# Navidrome is stopped, start tracks sync
airdrome navidrome push  # tracks, ratings, scrobbles
airdrome navidrome playlists  # playlists

# Then, start Navidrome again
