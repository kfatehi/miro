#/bin/bash
# Miro - an RSS based video player application
# Copyright (C) 2005-2009 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.


# =============================================================================

./setup_binarykit.sh
BKIT_VERSION="$(cat binary_kit_version)"

# =============================================================================

OS_VERSION=$(uname -r | cut -d . -f 1)

if [ $OS_VERSION == "9" ]; then
    TARGET_OS_VERSION=10.5
    PYTHON_VERSION=2.5
elif [ $OS_VERSION == "10" ]; then
    TARGET_OS_VERSION=10.6
    PYTHON_VERSION=2.6
else
    echo "## This script can only build a sandbox under Mac OS X 10.5 and 10.6."
    exit
fi

# =============================================================================

echo "** Building Miro sandbox for Mac OS X $TARGET_OS_VERSION."

ROOT_DIR=$(pushd ../../../ >/dev/null; pwd; popd >/dev/null)
BKIT_DIR=$(pwd)/miro-binary-kit-osx-$BKIT_VERSION/sandbox
SBOX_DIR=$ROOT_DIR/sandbox
SITE_DIR=$SBOX_DIR/lib/python$PYTHON_VERSION/site-packages
WORK_DIR=$SBOX_DIR/pkg

mkdir $SBOX_DIR
mkdir $WORK_DIR
mkdir -p $SITE_DIR

# Python ======================================================================

export VERSIONER_PYTHON_VERSION=$PYTHON_VERSION
export VERSIONER_PYTHON_PREFER_32_BIT=yes

PYTHON_ROOT=/System/Library/Frameworks/Python.framework/Versions/$PYTHON_VERSION
PYTHON=$PYTHON_ROOT/bin/python

echo "** Using Python $PYTHON_VERSION"

# =============================================================================

SDK_DIR="/Developer/SDKs/MacOSX$TARGET_OS_VERSION.sdk"

export CFLAGS="-mmacosx-version-min=$TARGET_OS_VERSION -isysroot $SDK_DIR -arch ppc -arch i386"
export LDFLAGS=$CFLAGS

# Pyrex =======================================================================

cd $WORK_DIR

tar -xzf $BKIT_DIR/Pyrex-0.9.8.5.tar.gz
cd $WORK_DIR/Pyrex-0.9.8.5

$PYTHON setup.py build
$PYTHON setup.py install --prefix=$SBOX_DIR

# Psyco =======================================================================

cd $WORK_DIR
svn co http://codespeak.net/svn/psyco/dist/ psyco
cd $WORK_DIR/psyco

$PYTHON setup.py build
$PYTHON setup.py install --prefix=$SBOX_DIR

# Boost =======================================================================

cd $WORK_DIR

BOOST_VERSION=1_39
BOOST_VERSION_FULL=1_39_0

tar -xzf $BKIT_DIR/boost_$BOOST_VERSION_FULL.tar.gz
cd boost_$BOOST_VERSION_FULL

cd tools/jam/src
./build.sh
cd `find . -type d -maxdepth 1 | grep bin.`
mkdir -p $SBOX_DIR/bin
cp bjam $SBOX_DIR/bin

cd $WORK_DIR/boost_$BOOST_VERSION_FULL
$SBOX_DIR/bin/bjam  --prefix=$SBOX_DIR \
                    --with-python \
                    --with-date_time \
                    --with-filesystem \
                    --with-thread \
                    --with-regex \
                    toolset=darwin \
                    macosx-version=$TARGET_OS_VERSION \
                    architecture=combined \
                    link=static \
                    release \
                    install

export BOOST_ROOT=$WORK_DIR/boost_$BOOST_VERSION_FULL/

# Libtorrent ===================================================================

cd $WORK_DIR

USER_CONFIG=`find $BOOST_ROOT -name user-config.jam`
echo "using python : $PYTHON_VERSION ;" >> $USER_CONFIG

tar -xvf $BKIT_DIR/libtorrent-rasterbar-*
cd libtorrent-rasterbar-*/bindings/python

$SBOX_DIR/bin/bjam --prefix=$SBOX_DIR \
    dht-support=on \
    toolset=darwin \
    macosx-version=$TARGET_OS_VERSION \
    architecture=combined \
    boost=source \
    boost-link=static \
    release

# Boost does not know how to correctly build a loadable module under OS X, it
# uses the -dynamiclib parameter when linking the module instead of -bundle, so
# we need to relink here.

echo "** Relinking the libtorrent module using the correct set of parameters"

LT_PYTHON_MOD_OBJS=$(find bin -name "*.o" -print)
LT_ARCHIVE=$(find ../../bin -name libtorrent.a -print)
BOOST_PYTHON_ARCHIVE=$(find $BOOST_ROOT -name libboost_python-*.a -print)
BOOST_THREAD_ARCHIVE=$(find $BOOST_ROOT -name libboost_thread-*.a -print)
BOOST_SYSTEM_ARCHIVE=$(find $BOOST_ROOT -name libboost_system-*.a -print)
BOOST_FILESYSTEM_ARCHIVE=$(find $BOOST_ROOT -name libboost_filesystem-*.a -print)

g++ -bundle \
    -Wl,-single_module \
    -Wl,-dead_strip \
    -L"$PYTHON_ROOT/lib" \
    -L"$PYTHON_ROOT/lib/python$PYTHON_VERSION/config" \
    -o $SITE_DIR/libtorrent.so \
    -lpython$PYTHON_VERSION \
    -lssl \
    -lcrypto \
    -headerpad_max_install_names \
    -no_dead_strip_inits_and_terms \
    -isysroot $SDK_DIR \
    -arch i386 \
    -arch ppc \
    $BOOST_PYTHON_ARCHIVE \
    $BOOST_THREAD_ARCHIVE \
    $BOOST_SYSTEM_ARCHIVE \
    $BOOST_FILESYSTEM_ARCHIVE \
    $LT_ARCHIVE \
    $LT_PYTHON_MOD_OBJS

echo "Done."
