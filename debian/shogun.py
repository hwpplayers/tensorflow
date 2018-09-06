#!/usr/bin/python3.6
# TF-Shogun: Distribution Friendly Light-Weight Build for TensorFlow.
#
#Copyright: 2018 Mo Zhou <lumin@debian.org>
#License: Expat
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
# .
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
# .
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, 
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE 
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

'''
TF-Shogun
=========

Distribution friendly light-weight build for TensorFlow.
Especially written for Debian GNU/Linux.

Shogun needs the bazel dumps from bazelQuery.sh .
And it tries to produce similar result to that from bazel.

References
----------

1. CMake build, tensorflow/contrib/cmake
2. Makefile build, tensorflow/contrib/makefile
3. TensorFlow's native Bazel build.
4. For extra compiler definitions .e.g TENSORFLOW_USE_JEMALLOC please lookup
   tensorflow/core/platform/default/build_config.bzl
5. ArchLinux PKGBUILD
   https://git.archlinux.org/svntogit/community.git/tree/trunk/PKGBUILD?h=packages/tensorflow
6. Gentoo ebuild
   https://packages.gentoo.org/packages/sci-libs/tensorflow
'''

from typing import *
import sys
import re
import os
import argparse
import json
import glob
import subprocess
from pprint import pprint
from ninja_syntax import Writer


# FIXME: don't forget to bump soversion when upstream version changes!
tf_soversion = '1.10'


def ninjaCommonHeader(cursor: Writer, ag: Any) -> None:
    '''
    Writes a common header to the ninja file. ag is parsed arguments.
    '''
    cursor.comment('-- start common ninja header --')
    cursor.comment(f'Note, this ninja file was automatically generated by {__file__}')
    cursor.newline()
    cursor.comment('-- compiling tools --')
    cursor.newline()
    cursor.variable('CXX', 'g++')
    cursor.variable('PROTOC', '/usr/bin/protoc')
    cursor.variable('PROTO_TEXT', f'./proto_text')
    cursor.variable('SHOGUN_EXTRA', '') # used for adding specific flags for a specific target
    cursor.newline()
    cursor.comment('-- compiler flags --')
    cursor.newline()
    cursor.variable('CPPFLAGS', '-D_FORTIFY_SOURCE=2 ' + str(os.getenv('CPPFLAGS', '')))
    cursor.variable('CXXFLAGS', '-std=c++14 -O2 -fPIC -gsplit-dwarf'
        + ' -fstack-protector-strong -w ' + str(os.getenv('CXXFLAGS', '')))
    cursor.variable('LDFLAGS', '-Wl,-z,relro ' + str(os.getenv('LDFLAGS', '')))
    cursor.variable('INCLUDES', '-I. -I./debian/embedded/eigen/ -I./third_party/eigen3/'
            + ' -I/usr/include/gemmlowp -I/usr/include/jsoncpp -I/usr/include/llvm-c-7'
            + ' -I/usr/include/llvm-7 -Ithird_party/toolchains/gpus/cuda/')
    cursor.variable('LIBS', '-lpthread -lprotobuf -lnsync -lnsync_cpp -ldouble-conversion'
	+ ' -ldl -lm -lz -lre2 -ljpeg -lpng -lsqlite3 -llmdb -lsnappy -lgif -lLLVM-7')
    cursor.newline()
    cursor.comment('-- compiling rules-- ')
    cursor.rule('rule_PROTOC', f'$PROTOC $in --cpp_out . $SHOGUN_EXTRA')
    cursor.rule('rule_PROTOC_GRPC', f'$PROTOC --grpc_out . --cpp_out . --plugin protoc-gen-grpc=/usr/bin/grpc_cpp_plugin $in')
    cursor.rule('rule_PROTO_TEXT', f'$PROTO_TEXT tensorflow/core tensorflow/core tensorflow/tools/proto_text/placeholder.txt $in')
    cursor.rule('rule_CXX_OBJ', f'$CXX $CPPFLAGS $CXXFLAGS $INCLUDES $SHOGUN_EXTRA -c $in -o $out')
    cursor.rule('rule_CXX_EXEC', f'$CXX $CPPFLAGS $CXXFLAGS $INCLUDES $LDFLAGS $LIBS $SHOGUN_EXTRA $in -o $out')
    cursor.rule('rule_CXX_SHLIB', f'$CXX -shared -fPIC $CPPFLAGS $CXXFLAGS $INCLUDES $LDFLAGS $LIBS $SHOGUN_EXTRA $in -o $out')
    cursor.rule('rule_CC_OP_GEN', f'LD_LIBRARY_PATH=. ./$in $out $cc_op_gen_internal tensorflow/core/api_def/base_api')
    cursor.rule('COPY', f'cp $in $out')
    cursor.newline()
    cursor.comment('-- end common ninja header --')
    cursor.newline()


def cyan(s: str) -> str:
    return f'\033[1;36m{s}\033[0;m'

def yellow(s: str) -> str:
    return f'\033[1;33m{s}\033[0;m'

def red(s: str) -> str:
    return f'\033[1;31m{s}\033[0;m'


def eGrep(pat: str, sourcelist: List[str]) -> (List[str], List[str]):
    '''
    Just like grep -E
    '''
    match, unmatch = [], []
    for item in sourcelist:
        if re.match(pat, item):
            match.append(item)
        else:
            unmatch.append(item)
    return match, unmatch


def eGlob(pat: str, *, filt: List[str] = [], vfilt: List[str] = []) -> List[str]:
    '''
    Extended version of glob.glob, which globs file and apply
    filters and reverse filters on the result.
    '''
    globs = glob.glob(pat, recursive=True)
    for f in filt:
        globs, _ = eGrep(f, globs)
    for vf in vfilt:
        _, globs = eGrep(f, globs)
    return globs


def getDpkgArchitecture(query: str) -> str:
    '''
    dpkg-architecture -qQUERY
    '''
    # XXX: I wish we don't need to use this function.
    raise Exception("Please try to keep TF functionality the same across different architectures.")
    result = subprocess.Popen(['dpkg-architecture', f'-q{query}'],
             stdout=subprocess.PIPE).communicate()[0].decode().strip()
    return result


def bazelPreprocess(srclist: List[str]) -> List[str]:
    '''
    1. Filter out external dependencies from bazel dependency dump.
    2. Mangle file path.
    3. Report the depending libraries.
    '''
    deplist, retlist = set([]), []
    for src in srclist:
        if re.match('^@(\w*).*', src):
            # It's an external dependency
            deplist.update(re.match('^@(\w*).*', src).groups())
        elif re.match('^..third_party.*', src):
            pass # ignore
        else:
            # it's an tensorflow source
            retlist.append(re.sub('^//', '', re.sub(':', '/', src)))
    print(cyan('Required Depends:'))
    pprint(deplist, indent=4, compact=True)
    print('Globbed', cyan(f'{len(srclist)}'), 'source files')
    return retlist


def shogunAllProto(argv):
    '''
    Generate XXX.pb.{h,cc} files from all available XXX.proto
    files in the source directory.

    Depends: protoc (protobuf-compiler)
    Input: .proto
    Output: .pb.cc, .pb.h
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-o', help='write ninja file', type=str, default='all_proto.ninja')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (1) initialize ninja file
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) glob all proto
    protos = glob.glob(f'**/*.proto', recursive=True)
    print(cyan('AllProto:'), f'globbed {len(protos)} .proto files')

    # (3) generate .pb.cc, .pb.h
    for proto in protos:
        cursor.build([ proto.replace('.proto', '.pb.cc'),
            proto.replace('.proto', '.pb.h')], 'rule_PROTOC', proto)

    # done
    cursor.close()


def shogunProtoText(argv):
    '''
    Build a binary ELF executable named proto_text, which generates
    XXX.pb_text{.cc,.h,-impl.h} files from a given XXX.proto file.
    This binary file is for one-time use.

    Depends: shogunAllProto
    Input: bazelDump, cxx source
    Output: proto_text
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-i', help='list of source files', type=str, required=True)
    ag.add_argument('-g', help='list of generated files', type=str, required=True)
    ag.add_argument('-o', help='where to write the ninja file', type=str, default='proto_text.ninja')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (0) read bazel dump and apply hardcoded filters
    srclist = bazelPreprocess([l.strip() for l in open(ag.i, 'r').readlines()])
    genlist = bazelPreprocess([l.strip() for l in open(ag.g, 'r').readlines()])

    # (1) Instantiate ninja writer
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) deal with generated files
    # (2.1) .pb.cc and .pb.h files are generated in shogunAllProto
    _, genlist = eGrep('.*.pb.h$', genlist)
    pbcclist, genlist = eGrep('.*.pb.cc$', genlist)
    if len(genlist) > 0:
        print(yellow('Remainders:'), genlist)

    # (3) deal with source files
    # (3.1) filter-out not needed files
    _, srclist = eGrep('.*.h$', srclist) # we don't need to deal with header here
    _, srclist = eGrep('^third_party', srclist) # no third_party stuff
    _, srclist = eGrep('.*windows/.*', srclist) # no windoge source
    _, srclist = eGrep('.*.proto$', srclist) # already dealt with in (2)

    # (3.2) compile .cc source
    cclist, srclist = eGrep('.*.cc', srclist)
    objlist = []
    for cc in cclist + pbcclist:
        obj = cc.replace('.cc', '.o')
        objlist.append(cursor.build(obj, 'rule_CXX_OBJ', cc)[0])
    if len(srclist) > 0:
        print(yellow('Remainders:'), srclist)

    # (4) link objects into the final ELF
    cursor.build(f'proto_text', 'rule_CXX_EXEC', inputs=objlist,
            variables={'LIBS': '-lpthread -lprotobuf -ldouble-conversion'})

    # done
    cursor.close()


def shogunTFLib_framework(argv):
    '''
    Build libtensorflow_framework.so. With slight modification, this
    function should be able to build libtensorflow_android.so too.

    Depends: AllProto, proto_text
    Input: bazelDump, cxx source
    Output: libtensorflow_framework.so
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-i', help='list of source files', type=str, required=True)
    ag.add_argument('-g', help='list of generated files', type=str, required=True)
    ag.add_argument('-o', help='where to write the ninja file', type=str, default='libtensorflow_framework.ninja')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (0) read bazel dump and apply hardcoded filters
    srclist = bazelPreprocess([l.strip() for l in open(ag.i, 'r').readlines()])
    genlist = bazelPreprocess([l.strip() for l in open(ag.g, 'r').readlines()])
    _, srclist = eGrep('.*proto_text.gen_proto_text_functions.*', srclist)
    _, srclist = eGrep('.*core.kernels.*', srclist)
    _, srclist = eGrep('.*core.ops.*', srclist)
    _, srclist = eGrep('^third_party', srclist)
    _, srclist = eGrep('.*/windows/.*', srclist) # no windoge source.
    _, srclist = eGrep('.*cc_op_gen.*', srclist) # don't include cc_op_gen.

    # (1) Initialize ninja file
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) deal with generated files
    # (2.1) .pb.h and .pb.cc are already generated by shogunAllProto
    gen_pbh, genlist = eGrep('.*.pb.h', genlist)
    gen_pbcc, genlist = eGrep('.*.pb.cc', genlist)

    # (2.2) .pb_text.*
    pbtlist = [x for x in genlist if any(x.endswith(y) for y in ('.pb_text.h', '.pb_text.cc', '.pb_text-impl.h'))]
    pbtlist = [x.replace('.pb_text.h', '.proto').replace('.pb_text.cc', '.proto').replace('.pb_text-impl.h', '.proto') for x in pbtlist]
    gen_pbth, genlist = eGrep('.*.pb_text.h', genlist)
    gen_pbtih, genlist = eGrep('.*.pb_text-impl.h', genlist)
    gen_pbtcc, genlist = eGrep('.*.pb_text.cc', genlist)
    for pbt in list(set(pbtlist)):
        cursor.build([
            pbt.replace('.proto', '.pb_text.h'),
            pbt.replace('.proto', '.pb_text.cc'),
            pbt.replace('.proto', '.pb_text-impl.h')
            ], 'rule_PROTO_TEXT', pbt)

    # (2.3) finish dealing with generated files
    if genlist:
        print(yellow('Remainders:'), genlist)
        assert(len(genlist) == 1)

    # (3) deal with source files
    # (3.1) filter-out headers
    _, srclist = eGrep('.*.proto$', srclist) # done in (2)
    src_hdrs, srclist = eGrep('.*.h$', srclist)

    # (3.2) compile .cc source
    src_cc, srclist = eGrep('.*.cc', srclist)
    objlist = []
    for cc in src_cc + gen_pbcc + gen_pbtcc + genlist:
        variables = {}
        if any(x in cc for x in ('posix/port.cc',)):
            #variables = {'SHOGUN_EXTRA': '-DTENSORFLOW_USE_JEMALLOC'}
            pass
        obj = cursor.build(cc.replace('.cc', '.o'), 'rule_CXX_OBJ', inputs=cc, variables=variables)[0]
        objlist.append(obj)

    # (4) link the final executable
    cursor.build('libtensorflow_framework.so', 'rule_CXX_SHLIB', inputs=objlist,
            variables={'LIBS': '-lfarmhash -lhighwayhash -lsnappy -lgif'
            + ' -ldouble-conversion -lz -lprotobuf -ljpeg -lnsync -lnsync_cpp'
            + ' -lpthread',
            'SHOGUN_EXTRA': f'-Wl,--soname=libtensorflow_framework.so.{tf_soversion}'
            + f' -Wl,--version-script tensorflow/tf_framework_version_script.lds'})
    # XXX: -ljemalloc FTBFS

    # done
    cursor.close()


def shogunCCOP(argv):
    '''
    Generate tensorflow cc ops : tensorflow/cc/ops/*.cc and *.h

    Depends: AllProto, proto_text, libtensorflow_framework
    Input: cc source, bazel dump
    Output: one-time-use binary "XXX_gen_cc" and generated .cc .h files.
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-i', help='list of source files', type=str, required=True)
    ag.add_argument('-g', help='list of generated files', type=str, required=True)
    ag.add_argument('-o', help='where to write the ninja file', type=str, default='ccop.ninja')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (0) read bazel dump and apply hardcoded filters
    genlist = bazelPreprocess([l.strip() for l in open(ag.g, 'r').readlines()])

    # (1) Instantiate ninja writer
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) filter unrelated files, we only want cc_op related files.
    _, genlist = eGrep('.*.pb.h', genlist)
    _, genlist = eGrep('.*.pb.cc', genlist)
    _, genlist = eGrep('.*.pb_text.h', genlist)
    _, genlist = eGrep('.*.pb_text-impl.h', genlist)
    _, genlist = eGrep('.*.pb_text.cc', genlist)

    # (3) XXX_gen_cc
    # (3.1) deal with a missing source
    cursor.build('tensorflow/core/ops/user_ops.cc', 'COPY', inputs='tensorflow/core/user_ops/fact.cc')

    # (3.2) build several common objects
    main_cc = ['tensorflow/core/framework/op_gen_lib.cc',
        'tensorflow/cc/framework/cc_op_gen.cc',
        'tensorflow/cc/framework/cc_op_gen_main.cc',
        ]
    main_obj = [x.replace('.cc', '.o') for x in main_cc]
    for cc in main_cc:
        cursor.build(cc.replace('.cc', '.o'), 'rule_CXX_OBJ', inputs=cc)

    # (3.2) build executables and generate file with executable
    gen_ccopcc, genlist = eGrep('.*/cc/ops/.*.cc', genlist)
    gen_ccoph, genlist = eGrep('.*/cc/ops/.*.h', genlist)
    opnamelist = list(set(os.path.basename(x.replace('.cc', '').replace('.h', ''))
        for x in (gen_ccopcc + gen_ccoph) if 'internal' not in x ))

    for opname in opnamelist:
        coreopcc = 'tensorflow/core/ops/' + opname + '.cc'
        ccopcc   = 'tensorflow/cc/ops/'   + opname + '.cc'

        # build corresponding elf executable
        cursor.build(f'{opname}_gen_cc', 'rule_CXX_EXEC', inputs=[coreopcc] + main_obj,
            variables={'SHOGUN_EXTRA': '-I. -L. -ltensorflow_framework'})

        # generate file
        cursor.build([ccopcc.replace('.cc', '.h'), ccopcc], 'rule_CC_OP_GEN', inputs=f'{opname}_gen_cc',
                variables={'cc_op_gen_internal': '0' if opname != 'sendrecv_ops' else '1'},
                implicit_outputs=[ccopcc.replace('.cc', '_internal.h'), ccopcc.replace('.cc', '_internal.cc')])

    ## done
    cursor.close()


def shogunTFLib(argv):
    '''
    Build libtensorflow.so

    Depends: all_proto, proto_text, CCOP
    Input: bazel dump, source files
    Output: libtensorflow.so
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-i', help='list of source files', type=str, required=True)
    ag.add_argument('-g', help='list of generated files', type=str, required=True)
    ag.add_argument('-o', help='where to write the ninja file', type=str, default='libtensorflow.ninja')
    ag.add_argument('-H', help='where to put the headers list', type=str, default='libtensorflow.hdrs')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (0) read bazel dump and apply hard-coded filters
    srclist = bazelPreprocess([l.strip() for l in open(ag.i, 'r').readlines()])
    genlist = bazelPreprocess([l.strip() for l in open(ag.g, 'r').readlines()])
    #srclist.extend(glob.glob('tensorflow/c/**.cc', recursive=True))
    #srclist.extend(glob.glob('tensorflow/cc/**.cc', recursive=True))
    tflib_extra_srcs = ['debian/embedded/fft/fftsg.c']
    _, srclist = eGrep('^third_party', srclist)
    _, srclist = eGrep('.*/windows/.*', srclist) # no windoge source.
    _, srclist = eGrep('.*.cu.cc$', srclist) # no CUDA file for CPU-only build
    _, srclist = eGrep('.*.pbtxt$', srclist) # not for us
    _, srclist = eGrep('.*platform/cloud.*', srclist) # SSL 1.1.1 broke it.
    _, srclist = eGrep('.*platform/s3.*', srclist) # we don't have https://github.com/aws/aws-sdk-cpp
    _, srclist = eGrep('.*_main.cc$', srclist) # don't include any main function.
    _, srclist = eGrep('.*test.*', srclist) # don't include any test
    _, srclist = eGrep('.*cc_op_gen.*', srclist) # don't include cc_op_gen.
    _, srclist = eGrep('.*gen_proto_text_functions.*', srclist) # not for this library
    _, srclist = eGrep('.*tensorflow.contrib.cloud.*', srclist) # it wants GoogleAuthProvider etc.
    _, srclist = eGrep('.*gcs_config_ops.cc', srclist) # it wants GcsFileSystem
    srclist = list(set(srclist))

    #if getDpkgArchitecture('DEB_HOST_ARCH') != 'amd64':
    if False:
        # the following stuff seems to be hard to compile
        _, srclist = eGrep('.*/core/debug/.*', srclist)
        _, genlist = eGrep('.*/core/debug/.*', genlist)
        _, srclist = eGrep('.*/compiler/.*', srclist)
        _, genlist = eGrep('.*/compiler/.*', genlist)
        _, srclist = eGrep('.*debug_ops.*', srclist)
        _, genlist = eGrep('.*debug_ops.*', genlist)

    # (1) Instantiate ninja writer
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) deal with generated files
    # (2.1) .pb.h and .pb.cc are already generated by shogunAllProto
    gen_pbh, genlist = eGrep('.*.pb.h', genlist)
    gen_pbcc, genlist = eGrep('.*.pb.cc', genlist)

    # XXX: temporary workaround for //tensorflow/core/debug:debug_service.grpc.pb.cc
    #if getDpkgArchitecture('DEB_HOST_ARCH') == 'amd64':
    if True:
        # This is amd64-only
        cursor.build(['tensorflow/core/debug/debug_service.grpc.pb.cc', 'tensorflow/core/debug/debug_service.grpc.pb.h'],
            'rule_PROTOC_GRPC', inputs='tensorflow/core/debug/debug_service.proto')

    # (2.2) .pb_text.*
    pbtlist = [x for x in genlist if any(x.endswith(y) for y in ('.pb_text.h', '.pb_text.cc', '.pb_text-impl.h'))]
    pbtlist = [x.replace('.pb_text.h', '.proto').replace('.pb_text.cc', '.proto').replace('.pb_text-impl.h', '.proto') for x in pbtlist]
    gen_pbth, genlist = eGrep('.*.pb_text.h', genlist)
    gen_pbtih, genlist = eGrep('.*.pb_text-impl.h', genlist)
    gen_pbtcc, genlist = eGrep('.*.pb_text.cc', genlist)
    for pbt in list(set(pbtlist)):
        cursor.build([
            pbt.replace('.proto', '.pb_text.h'),
            pbt.replace('.proto', '.pb_text.cc'),
            pbt.replace('.proto', '.pb_text-impl.h')
            ], 'rule_PROTO_TEXT', pbt)

    # (2.3) cc_op_gen
    gen_ccopcc, genlist = eGrep('.*/cc/ops/.*.cc', genlist)
    gen_ccoph, genlist = eGrep('.*/cc/ops/.*.h', genlist)

    # (2.4) finish dealing with generated files
    if genlist:
        print(yellow('Remainders:'), genlist)
        assert(len(genlist) == 1)

    # (3) deal with source files
    # (3.1) filter-out headers
    _, srclist = eGrep('.*.proto$', srclist) # done in (2)
    src_hdrs, srclist = eGrep('.*.h$', srclist)

    # (3.2) compile .cc source
    src_cc, srclist = eGrep('.*.cc', srclist)
    objlist = []
    exception_eigen_avoid_std_array = [
        'sparse_tensor_dense_matmul_op', 'conv_grad_ops_3d', 'adjust_contrast_op' ]
    for cc in src_cc + gen_pbcc + gen_pbtcc + gen_ccopcc + genlist + tflib_extra_srcs:
        variables = {}
        if any(x in cc for x in ('posix/port.cc',)):
            #variables = {'SHOGUN_EXTRA': '-DTENSORFLOW_USE_JEMALLOC'}
            pass
        elif any(x in cc for x in exception_eigen_avoid_std_array):
            variables = {'SHOGUN_EXTRA': '-DEIGEN_AVOID_STL_ARRAY'}
        obj = cursor.build(re.sub('.c[c]?$', '.o', cc), 'rule_CXX_OBJ', inputs=cc, variables=variables)[0]
        objlist.append(obj)

    # (4) link the final shared object
    cursor.build('libtensorflow.so', 'rule_CXX_SHLIB', inputs=objlist,
            variables={'LIBS': '-lpthread -lprotobuf -lnsync -lnsync_cpp'
                + ' -ldouble-conversion -ljpeg -lpng -lgif -lhighwayhash'
                + ' -lfarmhash -ljsoncpp -lsqlite3 -lre2 -lcurl'
                + ' -llmdb -lsnappy -ldl -lz -lm -lLLVM-7 -lgrpc++',
                'SHOGUN_EXTRA': f'-Wl,--soname=libtensorflow.so.{tf_soversion}'
                + f' -Wl,--version-script tensorflow/c/version_script.lds'})
    # FIXME: mkl-dnn, grpc, xsmm
    # XXX: FTBFS with jemalloc

    # (5) write down the related header files
    allHdrs = gen_pbh + gen_pbth + gen_pbtih + gen_ccoph + src_hdrs
    with open(ag.H, 'w') as f:
        f.writelines([x + '\n' for x in sorted(allHdrs)])

    # done
    print(yellow('Remainders:'))
    pprint(srclist, indent=4, compact=4)
    cursor.close()


def shogunTFCCLib(argv):
    '''
    Build libtensorflow_cc.so

    Depends: all_proto, proto_text, CCOP
    Input: bazel dump, source files
    Output: libtensorflow_cc.so
    '''
    ag = argparse.ArgumentParser()
    ag.add_argument('-i', help='list of source files', type=str, required=True)
    ag.add_argument('-g', help='list of generated files', type=str, required=True)
    ag.add_argument('-o', help='where to write the ninja file', type=str, default='libtensorflow_cc.ninja')
    ag.add_argument('-H', help='where to put the headers list', type=str, default='libtensorflow_cc.hdrs')
    ag = ag.parse_args(argv)
    print(red('Argument Dump:'))
    pprint(vars(ag))

    # (0) read bazel dump and apply hard-coded filters
    srclist = bazelPreprocess([l.strip() for l in open(ag.i, 'r').readlines()])
    genlist = bazelPreprocess([l.strip() for l in open(ag.g, 'r').readlines()])
    #srclist.extend(glob.glob('tensorflow/c/**.cc', recursive=True))
    #srclist.extend(glob.glob('tensorflow/cc/**.cc', recursive=True))
    tflib_extra_srcs = ['debian/embedded/fft/fftsg.c']
    _, srclist = eGrep('^third_party', srclist)
    _, srclist = eGrep('.*/windows/.*', srclist) # no windoge source.
    _, srclist = eGrep('.*.cu.cc$', srclist) # no CUDA file for CPU-only build
    _, srclist = eGrep('.*.pbtxt$', srclist) # not for us
    _, srclist = eGrep('.*platform/cloud.*', srclist) # SSL 1.1.1 broke it.
    _, srclist = eGrep('.*platform/s3.*', srclist) # we don't have https://github.com/aws/aws-sdk-cpp
    _, srclist = eGrep('.*_main.cc$', srclist) # don't include any main function.
    _, srclist = eGrep('.*test.*', srclist) # don't include any test
    _, srclist = eGrep('.*cc_op_gen.*', srclist) # don't include cc_op_gen.
    _, srclist = eGrep('.*gen_proto_text_functions.*', srclist) # not for this library
    _, srclist = eGrep('.*tensorflow.contrib.cloud.*', srclist) # it wants GoogleAuthProvider etc.
    _, srclist = eGrep('.*gcs_config_ops.cc', srclist) # it wants GcsFileSystem
    srclist = list(set(srclist))

    #if getDpkgArchitecture('DEB_HOST_ARCH') != 'amd64':
    if False:
        # the following stuff seems to be hard to compile
        _, srclist = eGrep('.*/core/debug/.*', srclist)
        _, genlist = eGrep('.*/core/debug/.*', genlist)
        _, srclist = eGrep('.*/compiler/.*', srclist)
        _, genlist = eGrep('.*/compiler/.*', genlist)
        _, srclist = eGrep('.*debug_ops.*', srclist)
        _, genlist = eGrep('.*debug_ops.*', genlist)

    # (1) Instantiate ninja writer
    cursor = Writer(open(ag.o, 'w'))
    ninjaCommonHeader(cursor, ag)

    # (2) deal with generated files
    # (2.1) .pb.h and .pb.cc are already generated by shogunAllProto
    gen_pbh, genlist = eGrep('.*.pb.h', genlist)
    gen_pbcc, genlist = eGrep('.*.pb.cc', genlist)

    # XXX: temporary workaround for //tensorflow/core/debug:debug_service.grpc.pb.cc
    #if getDpkgArchitecture('DEB_HOST_ARCH') == 'amd64':
    if True:
        # This is amd64-only
        cursor.build(['tensorflow/core/debug/debug_service.grpc.pb.cc', 'tensorflow/core/debug/debug_service.grpc.pb.h'],
            'rule_PROTOC_GRPC', inputs='tensorflow/core/debug/debug_service.proto')

    # (2.2) .pb_text.*
    pbtlist = [x for x in genlist if any(x.endswith(y) for y in ('.pb_text.h', '.pb_text.cc', '.pb_text-impl.h'))]
    pbtlist = [x.replace('.pb_text.h', '.proto').replace('.pb_text.cc', '.proto').replace('.pb_text-impl.h', '.proto') for x in pbtlist]
    gen_pbth, genlist = eGrep('.*.pb_text.h', genlist)
    gen_pbtih, genlist = eGrep('.*.pb_text-impl.h', genlist)
    gen_pbtcc, genlist = eGrep('.*.pb_text.cc', genlist)
    for pbt in list(set(pbtlist)):
        cursor.build([
            pbt.replace('.proto', '.pb_text.h'),
            pbt.replace('.proto', '.pb_text.cc'),
            pbt.replace('.proto', '.pb_text-impl.h')
            ], 'rule_PROTO_TEXT', pbt)

    # (2.3) cc_op_gen
    gen_ccopcc, genlist = eGrep('.*/cc/ops/.*.cc', genlist)
    gen_ccoph, genlist = eGrep('.*/cc/ops/.*.h', genlist)

    # (2.4) finish dealing with generated files
    if genlist:
        print(yellow('Remainders:'), genlist)
        assert(len(genlist) == 1)

    # (3) deal with source files
    # (3.1) filter-out headers
    _, srclist = eGrep('.*.proto$', srclist) # done in (2)
    src_hdrs, srclist = eGrep('.*.h$', srclist)

    # (3.2) compile .cc source
    src_cc, srclist = eGrep('.*.cc', srclist)
    objlist = []
    exception_eigen_avoid_std_array = [
        'sparse_tensor_dense_matmul_op', 'conv_grad_ops_3d', 'adjust_contrast_op' ]
    for cc in src_cc + gen_pbcc + gen_pbtcc + gen_ccopcc + genlist + tflib_extra_srcs:
        variables = {}
        if any(x in cc for x in ('posix/port.cc',)):
            #variables = {'SHOGUN_EXTRA': '-DTENSORFLOW_USE_JEMALLOC'}
            pass
        elif any(x in cc for x in exception_eigen_avoid_std_array):
            variables = {'SHOGUN_EXTRA': '-DEIGEN_AVOID_STL_ARRAY'}
        obj = cursor.build(re.sub('.c[c]?$', '.o', cc), 'rule_CXX_OBJ', inputs=cc, variables=variables)[0]
        objlist.append(obj)

    # (4) link the final shared object
    cursor.build('libtensorflow_cc.so', 'rule_CXX_SHLIB', inputs=objlist,
            variables={'LIBS': '-lpthread -lprotobuf -lnsync -lnsync_cpp'
                + ' -ldouble-conversion -ljpeg -lpng -lgif -lhighwayhash'
                + ' -lfarmhash -ljsoncpp -lsqlite3 -lre2 -lcurl'
                + ' -llmdb -lsnappy -ldl -lz -lm -lLLVM-7 -lgrpc++',
                'SHOGUN_EXTRA': f'-Wl,--soname=libtensorflow_cc.so.{tf_soversion}'
                + f' -Wl,--version-script tensorflow/tf_version_script.lds'})
    # FIXME: mkl-dnn, grpc, xsmm
    # XXX: FTBFS with jemalloc

    # (5) write down the related header files
    allHdrs = gen_pbh + gen_pbth + gen_pbtih + gen_ccoph + src_hdrs
    with open(ag.H, 'w') as f:
        f.writelines([x + '\n' for x in sorted(allHdrs)])

    # done
    print(yellow('Remainders:'))
    pprint(srclist, indent=4, compact=4)
    cursor.close()


if __name__ == '__main__':

    # A graceful argparse implementation with argparse subparser requries
    # much more boring code than I would like to write.
    try:
        eval(f'shogun{sys.argv[1]}')(sys.argv[2:])
    except (IndexError, NameError) as e:
        print('you must specify one of the following a subcommand:')
        print([k.replace('shogun', '') for (k, v) in locals().items() if k.startswith('shogun')])
        exit(1)
