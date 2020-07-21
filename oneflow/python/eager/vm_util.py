from __future__ import absolute_import

import re
from contextlib import contextmanager

import oneflow as flow
import oneflow.core.eager.eager_symbol_pb2 as eager_symbol_util
import oneflow.core.job.placement_pb2 as placement_pb_util
import oneflow.core.operator.op_conf_pb2 as op_conf_util
import oneflow.core.operator.op_attribute_pb2 as op_attribute_pb
import oneflow.core.vm.instruction_pb2 as instr_util
import oneflow.python.eager.blob_cache as blob_cache_util
import oneflow.python.eager.boxing_util as boxing_util
import oneflow.python.eager.job_conf_ctx as job_conf_ctx
import oneflow.python.eager.object as object_util
import oneflow.python.eager.object_storage as object_storage
import oneflow.python.eager.symbol as symbol_util
import oneflow.python.eager.symbol_storage as symbol_storage
import oneflow.python.framework.c_api_util as c_api_util
import oneflow.python.framework.scope_util as scope_util
import oneflow.python.framework.id_util as id_util
import oneflow.python.framework.op_arg_util as op_arg_util
import oneflow.python.framework.placement_context as placement_ctx
import oneflow.python.framework.python_callback as python_callback
import oneflow.python.framework.session_context as session_ctx
import oneflow.python.vm.id_util as vm_id_util
from oneflow.python.oneflow_export import oneflow_export


def PhysicalRun(build):
    return _Run(
        build,
        vm_id_util.PhysicalIdGenerator(),
        c_api_util.RunPhysicalInstruction,
        _ReleasePhysicalBlobObject,
    )


def LogicalRun(build):
    return _Run(
        build,
        vm_id_util.LogicalIdGenerator(),
        c_api_util.RunLogicalInstruction,
        _ReleaseLogicalBlobObject,
    )


@session_ctx.try_init_default_session
@oneflow_export("test")
def GetOfBlobInRegst(var_name):
    def temp(builder):
        blob_object = builder.MakeLazyRefBlobObject(var_name)

        def fetcher(ofblob):
            print(ofblob.CopyToNdarray())

        builder.FetchBlobBody(blob_object, fetcher)

    LogicalRun(temp)


def _Run(build, id_generator, run_api, release_blob_object):
    instruction_list = session_ctx.GetDefaultSession().instruction_list
    eager_symbol_list = session_ctx.GetDefaultSession().eager_symbol_list
    build(
        InstructionsBuilder(
            id_generator, release_blob_object, instruction_list, eager_symbol_list
        )
    )
    run_api(instruction_list, eager_symbol_list)
    instruction_list.ClearField("instruction")
    eager_symbol_list.ClearField("eager_symbol")


def _DefaultBlobObject4Ibn(ibn):
    raise NotImplementedError


class InstructionsBuilder(object):
    def __init__(
        self, id_generator, release_blob_object, instruction_list, eager_symbol_list
    ):
        self.id_generator_ = id_generator
        self.release_blob_object_ = release_blob_object
        assert isinstance(instruction_list, instr_util.InstructionListProto)
        assert isinstance(eager_symbol_list, eager_symbol_util.EagerSymbolList)
        self.instruction_list_ = instruction_list
        self.eager_symbol_list_ = eager_symbol_list

    def StatelessCall(self, op_attribute, parallel_conf, bn_in_op2blob_object={}):
        op_parallel_desc_sym = self.GetParallelDescSymbol(parallel_conf)
        self._CheckRefInBlobObjectParallelDesc(
            op_attribute,
            op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )

        def GetDelegateBlobObject(blob_object, op_arg_parallel_attr):
            return _FindOrCreateDelegateBlobObject(
                self, blob_object, op_arg_parallel_attr
            )

        self._StatelessCall(
            "compute",
            op_attribute,
            op_parallel_desc_sym=op_parallel_desc_sym,
            blob_parallel_desc_sym=op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
            get_delegate_blob_object=GetDelegateBlobObject,
        )

    def BoxingStatelessCall(self, op_attribute, parallel_conf, bn_in_op2blob_object={}):
        op_parallel_desc_sym = self.GetParallelDescSymbol(parallel_conf)
        self._CheckRefInBlobObjectParallelDesc(
            op_attribute,
            op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )

        def GetDirectBlobObject(blob_object, op_arg_parallel_attr):
            return blob_object

        self._StatelessCall(
            "compute",
            op_attribute,
            op_parallel_desc_sym=op_parallel_desc_sym,
            blob_parallel_desc_sym=op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
            get_delegate_blob_object=GetDirectBlobObject,
        )

    def BoxingCudaD2HStatelessCall(
        self, op_attribute, in_parallel_conf, bn_in_op2blob_object={}
    ):
        op_parallel_desc_sym = self.GetParallelDescSymbol(in_parallel_conf)
        blob_parallel_desc_sym = boxing_util.TryReplaceDeviceTag(
            self, op_parallel_desc_sym, "cpu"
        )
        self._CheckRefInBlobObjectParallelDesc(
            op_attribute,
            blob_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )

        def GetDirectBlobObject(blob_object, op_arg_parallel_attr):
            return blob_object

        self._StatelessCall(
            "copy_d2h",
            op_attribute,
            op_parallel_desc_sym=op_parallel_desc_sym,
            blob_parallel_desc_sym=blob_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
            get_delegate_blob_object=GetDirectBlobObject,
        )

    def BoxingCudaH2DStatelessCall(
        self, op_attribute, out_parallel_conf, bn_in_op2blob_object={}
    ):
        op_parallel_desc_sym = self.GetParallelDescSymbol(out_parallel_conf)
        self._CheckRefInBlobObjectParallelDesc(
            op_attribute,
            op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )

        def GetDirectBlobObject(blob_object, op_arg_parallel_attr):
            return blob_object

        self._StatelessCall(
            "copy_h2d",
            op_attribute,
            op_parallel_desc_sym=op_parallel_desc_sym,
            blob_parallel_desc_sym=op_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
            get_delegate_blob_object=GetDirectBlobObject,
        )

    def _StatelessCall(
        self,
        stream_tag,
        op_attribute,
        op_parallel_desc_sym=None,
        blob_parallel_desc_sym=None,
        bn_in_op2blob_object={},
        get_delegate_blob_object=None,
    ):
        assert callable(get_delegate_blob_object)
        if op_attribute.parallel_signature.HasField("op_parallel_desc_symbol_id"):
            symbol_id = op_attribute.parallel_signature.op_parallel_desc_symbol_id
            op_parallel_desc_sym = symbol_storage.GetSymbol4Id(symbol_id)
        assert op_parallel_desc_sym is not None

        def DelegateBlobObject4Ibn(ibn):
            op_arg_parallel_attr = op_arg_util.GetOpArgParallelAttribute(
                op_parallel_desc_sym, op_attribute, ibn
            )
            return get_delegate_blob_object(
                bn_in_op2blob_object[ibn], op_arg_parallel_attr
            )

        job_conf_sym = self.GetJobConfSymbol(job_conf_ctx.CurrentJobConf())
        op_conf_sym = self._GetOpConfSymbol(op_attribute.op_conf)
        op_parallel_attribute_sym = self._GetOpParallelAttributeSymbol(op_attribute)
        opkernel_obj = self.GetSharedOpKernelObject4ParallelConfSymbol(
            op_parallel_desc_sym
        )
        const_operand_blob_objects = self._GetConstOperandBlobObjects(
            op_attribute, blob_object4ibn=DelegateBlobObject4Ibn
        )
        mut1_operand_blob_objects = self._GetMut1OperandBlobObjects(
            op_attribute,
            blob_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )
        mut2_operand_blob_objects = self._GetMut2OperandBlobObjects(
            op_attribute,
            blob_parallel_desc_sym,
            bn_in_op2blob_object=bn_in_op2blob_object,
        )
        is_user_op = op_attribute.op_conf.HasField("user_conf")
        instruction_prefix = "User" if is_user_op else "System"
        self._StatelessCallOpKernel(
            "%s.%sStatelessCallOpKernel" % (stream_tag, instruction_prefix),
            op_parallel_desc_sym,
            job_conf_sym,
            op_conf_sym,
            op_parallel_attribute_sym,
            opkernel_obj,
            const_operand_blob_objects,
            mut1_operand_blob_objects,
            mut2_operand_blob_objects,
        )

    def DeleteBlob(self, blob_object):
        self._TryClearObject(blob_object)
        self._DeleteObject(blob_object)

    def InsertRemoveForeignCallbackInstruction(self, object_id, callback):
        unique_callback_id = python_callback.GetIdForRegisteredCallback(callback)
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "RemoveForeignCallback"
        instruction.operand.append(_DelObjectOperand(object_id))
        instruction.operand.append(_Int64Operand(unique_callback_id))
        self.instruction_list_.instruction.append(instruction)

    def FetchBlobHeader(self, blob_object, callback):
        return self._FetchBlob("FetchBlobHeader", blob_object, callback)

    def FetchBlobBody(self, blob_object, callback):
        return self._FetchBlob("FetchBlobBody", blob_object, callback)

    def PackPhysicalBlobsToLogicalBlob(
        self, physical_blob_objects, op_arg_parallel_attr, op_arg_blob_attr
    ):
        parallel_desc_symbol = op_arg_parallel_attr.parallel_desc_symbol
        machine_id2device_ids = parallel_desc_symbol.machine_id2device_id_list
        _, device_tag, _ = parallel_desc_symbol.parallel_conf.device_name[0].split(":")
        machine_device_ids = set()
        for physical_blob_object in physical_blob_objects:
            phy_paralle_desc_sym = physical_blob_object.parallel_desc_symbol
            assert (
                phy_paralle_desc_sym.parallel_num == 1
            ), phy_paralle_desc_sym.parallel_num
            assert phy_paralle_desc_sym.device_tag == device_tag, "%s v.s. %s" % (
                phy_paralle_desc_sym.device_tag,
                device_tag,
            )
            phy_machine_id2device_ids = phy_paralle_desc_sym.machine_id2device_id_list
            machine_id = list(phy_machine_id2device_ids.keys())[0]
            pair = (machine_id, phy_machine_id2device_ids[machine_id][0])
            machine_device_ids.add(pair)

        for machine_id, device_ids in machine_id2device_ids.items():
            for device_id in device_ids:
                assert (machine_id, device_id) in machine_device_ids, "%s not in %s" % (
                    (machine_id, device_id),
                    machine_device_ids,
                )
        logical_blob_object = self._NewBlobObject(
            op_arg_parallel_attr, op_arg_blob_attr
        )
        self._ReplaceMirrored(
            op_arg_parallel_attr.parallel_desc_symbol,
            [logical_blob_object],
            physical_blob_objects,
        )
        return logical_blob_object

    def GetPhysicalParallelDescSymbols(self, parallel_desc_symbol):
        machine_id2device_ids = parallel_desc_symbol.machine_id2device_id_list
        _, device_tag, _ = parallel_desc_symbol.parallel_conf.device_name[0].split(":")
        phy_parallel_desc_symbols = []

        def AppendPhyParallelDescSymbol(machine_id, device_id):
            parallel_conf = placement_pb_util.ParallelConf()
            parallel_conf.device_name.append(
                "%d:%s:%d" % (machine_id, device_tag, device_id)
            )
            phy_parallel_desc_symbols.append(self.GetParallelDescSymbol(parallel_conf))

        for machine_id, device_ids in machine_id2device_ids.items():
            for device_id in device_ids:
                AppendPhyParallelDescSymbol(machine_id, device_id)
        return phy_parallel_desc_symbols

    def UnpackLogicalBlobToPhysicalBlobs(self, blob_object):
        phy_parallel_desc_symbols = self.GetPhysicalParallelDescSymbols(
            blob_object.parallel_desc_symbol
        )

        def GetPhysicalBlob(parallel_desc_sym):
            op_arg_parallel_attr = op_arg_util.MakeMirroredOpArgParallelAttribute(
                parallel_desc_sym
            )
            pyhsical_blob_object = self._NewBlobObject(
                op_arg_parallel_attr, blob_object.op_arg_blob_attr
            )
            return pyhsical_blob_object

        physical_blob_objects = [
            GetPhysicalBlob(symbol) for symbol in phy_parallel_desc_symbols
        ]
        self._ReplaceMirrored(
            blob_object.parallel_desc_symbol, physical_blob_objects, [blob_object]
        )
        return physical_blob_objects

    def MakeReferenceBlobObject(self, blob_object, op_arg_parallel_attr):
        parallel_desc_symbol = blob_object.parallel_desc_symbol
        assert parallel_desc_symbol == op_arg_parallel_attr.parallel_desc_symbol
        ref_blob_object = self._NewBlobObject(
            op_arg_parallel_attr, blob_object.op_arg_blob_attr
        )
        self._ReplaceMirrored(parallel_desc_symbol, [ref_blob_object], [blob_object])
        return ref_blob_object

    def MakeLazyRefBlobObject(self, var_name):
        sess = session_ctx.GetDefaultSession()
        op_attribute = sess.GetOpAttrFromVarName(var_name)
        obn = "out"

        blob_parallel_desc_sym_id = op_attribute.parallel_signature.bn_in_op2parallel_desc_symbol_id[
            obn
        ]
        blob_parallel_desc_sym = symbol_storage.GetSymbol4Id(blob_parallel_desc_sym_id)
        op_arg_parallel_attr = op_arg_util.GetOpArgParallelAttribute(
            blob_parallel_desc_sym, op_attribute, obn
        )
        op_arg_blob_attr = op_arg_util.GetOpArgBlobAttribute(op_attribute, obn)

        blob_object = self._NewBlobObject(op_arg_parallel_attr, op_arg_blob_attr)
        self._LazyReference(blob_object, var_name)
        return blob_object

    def GetSymbol4String(self, string):
        if symbol_storage.HasSymbol4String(string):
            return symbol_storage.GetSymbol4String(string)
        symbol_id = self._NewSymbolId4String(string)
        symbol = symbol_util.Symbol(symbol_id, string)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4String(string, symbol)
        return symbol

    def GetJobConfSymbol(self, job_conf):
        if symbol_storage.HasSymbol4JobConf(job_conf):
            return symbol_storage.GetSymbol4JobConf(job_conf)
        symbol_id = self._NewSymbolId4JobConf(job_conf)
        symbol = symbol_util.Symbol(symbol_id, job_conf)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4JobConf(job_conf, symbol)
        return symbol

    def GetParallelDescSymbol(self, parallel_conf):
        _, device_tag, _ = parallel_conf.device_name[0].split(":")
        serialized_parallel_conf = parallel_conf.SerializeToString()
        if symbol_storage.HasSymbol4SerializedParallelConf(serialized_parallel_conf):
            return symbol_storage.GetSymbol4SerializedParallelConf(
                serialized_parallel_conf
            )
        symbol_id = self._NewSymbolId4ParallelConf(parallel_conf)
        symbol = symbol_util.ParallelDescSymbol(symbol_id, parallel_conf, device_tag)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4SerializedParallelConf(
            serialized_parallel_conf, symbol
        )
        return symbol

    def GetScopeSymbol(self, scope_proto, parent_scope_symbol=None):
        symbol_id = self._NewSymbolId4Scope(scope_proto)
        serialized_scope_proto = scope_proto.SerializeToString()
        if symbol_storage.HasSymbol4SerializedScopeProto(serialized_scope_proto):
            return symbol_storage.GetSymbol4SerializedScopeProto(serialized_scope_proto)
        symbol = scope_util.ScopeSymbol(symbol_id, scope_proto, parent_scope_symbol)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4SerializedScopeProto(serialized_scope_proto, symbol)
        return symbol

    def GetSharedOpKernelObject4ParallelConfSymbol(self, parallel_desc_sym):
        if object_storage.HasSharedOpKernelObject4ParallelConfSymbol(parallel_desc_sym):
            return object_storage.GetSharedOpKernelObject4ParallelConfSymbol(
                parallel_desc_sym
            )
        object_id = self._NewSharedOpKernelObjectId4ParallelConfSymbolId(
            parallel_desc_sym
        )
        obj = object_util.Object(object_id, parallel_desc_sym)
        object_storage.SetSharedOpKernelObject4ParallelConfSymbol(
            parallel_desc_sym, obj
        )
        return obj

    @contextmanager
    def CudaHostPinBlob(self, blob_object):
        self._CudaHostRegisterBlob(blob_object)
        try:
            yield
        finally:
            self._CudaHostUnregisterBlob(blob_object)

    def BroadcastBlobReference(self, sole_mirrored_blob_object, parallel_desc_sym):
        device_ids = (
            sole_mirrored_blob_object.parallel_desc_symbol.machine_id2device_id_list
        )
        for _, dev_ids in device_ids.items():
            assert len(dev_ids) == 1, "dev_ids: %s" % dev_ids
        object_id = self._BroadcastObjectReference(
            sole_mirrored_blob_object, parallel_desc_sym
        )
        op_arg_parallel_attr = op_arg_util.MakeBroadcastOpArgParallelAttribute(
            parallel_desc_sym
        )
        return object_util.BlobObject(
            object_id=object_id,
            op_arg_parallel_attr=op_arg_parallel_attr,
            op_arg_blob_attr=sole_mirrored_blob_object.op_arg_blob_attr,
            release=self.release_blob_object_,
        )

    def _CudaHostRegisterBlob(self, blob_object):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "CudaHostRegisterBlob"
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_MutOperand(blob_object.object_id))
        self.instruction_list_.instruction.append(instruction)

    def _CudaHostUnregisterBlob(self, blob_object):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "CudaHostUnregisterBlob"
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_MutOperand(blob_object.object_id))
        self.instruction_list_.instruction.append(instruction)

    def _GetOpConfSymbol(self, op_conf):
        serialized_op_conf = op_conf.SerializeToString()
        if symbol_storage.HasSymbol4SerializedOpConf(serialized_op_conf):
            return symbol_storage.GetSymbol4SerializedOpConf(serialized_op_conf)
        symbol_id = self._NewSymbolId4OpConf(op_conf)
        symbol = symbol_util.Symbol(symbol_id, op_conf)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4SerializedOpConf(serialized_op_conf, symbol)
        return symbol

    def _GetOpParallelAttributeSymbol(self, op_attribute):
        new_op_parallel_attribute = op_attribute_pb.OpParallelAttribute()
        new_op_parallel_attribute.sbp_signature.CopyFrom(op_attribute.sbp_signature)
        new_op_parallel_attribute.mirrored_signature.CopyFrom(
            op_attribute.mirrored_signature
        )
        new_op_parallel_attribute.parallel_signature.CopyFrom(
            op_attribute.parallel_signature
        )
        serialized_op_parallel_attribute = new_op_parallel_attribute.SerializeToString()
        if symbol_storage.HasSymbol4SerializedOpParallelAttribute(
            serialized_op_parallel_attribute
        ):
            return symbol_storage.GetSymbol4SerializedOpParallelAttribute(
                serialized_op_parallel_attribute
            )
        symbol_id = self._NewSymbolId4OpParallelAttribute(new_op_parallel_attribute)
        symbol = symbol_util.Symbol(symbol_id, new_op_parallel_attribute)
        symbol_storage.SetSymbol4Id(symbol_id, symbol)
        symbol_storage.SetSymbol4SerializedOpParallelAttribute(
            serialized_op_parallel_attribute, symbol
        )
        return symbol

    def _GetConstOperandBlobObjects(self, op_attribute, blob_object4ibn=None):
        assert callable(blob_object4ibn)
        const_operand_blob_objects = []
        for ibn in op_attribute.input_bns:
            ibn2modifier = op_attribute.arg_modifier_signature.ibn2input_blob_modifier
            if ibn2modifier[ibn].is_mutable:
                continue
            ibn_sym = self.GetSymbol4String(ibn)
            in_object = blob_object4ibn(ibn)
            const_operand_blob_objects.append((ibn_sym, in_object))
        return const_operand_blob_objects

    def _GetMut1OperandBlobObjects(
        self, op_attribute, parallel_desc_sym, bn_in_op2blob_object={}
    ):
        mut1_operand_blob_objects = []
        for ibn in op_attribute.input_bns:
            ibn2modifier = op_attribute.arg_modifier_signature.ibn2input_blob_modifier
            if not ibn2modifier[ibn].is_mutable:
                continue
            ibn_sym = self.GetSymbol4String(ibn)
            ref_blob_object = bn_in_op2blob_object[ibn]
            mut1_operand_blob_objects.append((ibn_sym, ref_blob_object))

        def GetOutBlobParallelDescSymbol(obn):
            parallel_signature = op_attribute.parallel_signature
            bn2symbol_id = parallel_signature.bn_in_op2parallel_desc_symbol_id
            if obn in bn2symbol_id:
                return symbol_storage.GetSymbol4Id(bn2symbol_id[obn])
            else:
                return parallel_desc_sym

        def OutputBns():
            obn2modifier = op_attribute.arg_modifier_signature.obn2output_blob_modifier
            for obn in op_attribute.output_bns:
                if obn2modifier[obn].header_infered_before_compute:
                    yield obn

            for tmp_bn in op_attribute.tmp_bns:
                yield tmp_bn

        for obn in OutputBns():
            obn_sym = self.GetSymbol4String(obn)
            op_arg_parallel_attr = op_arg_util.GetOpArgParallelAttribute(
                GetOutBlobParallelDescSymbol(obn), op_attribute, obn
            )
            op_arg_blob_attr = op_arg_util.GetOpArgBlobAttribute(op_attribute, obn)
            out_blob_object = self._NewBlobObject(
                op_arg_parallel_attr, op_arg_blob_attr
            )
            lbi = op_attribute.arg_signature.bn_in_op2lbi[obn]
            bn_in_op2blob_object[obn] = out_blob_object
            mut1_operand_blob_objects.append((obn_sym, out_blob_object))
        return mut1_operand_blob_objects

    def _CheckRefInBlobObjectParallelDesc(
        self, op_attribute, op_parallel_desc_sym, bn_in_op2blob_object={}
    ):
        op_conf = op_attribute.op_conf
        for ibn in op_attribute.input_bns:
            ibn2modifier = op_attribute.arg_modifier_signature.ibn2input_blob_modifier
            if not ibn2modifier[ibn].is_mutable:
                continue
            ref_blob_object = bn_in_op2blob_object[ibn]
            assert op_parallel_desc_sym == ref_blob_object.parallel_desc_symbol, (
                "op_conf: %s\n%s\nv.s.\n%s"
                % (op_conf, op_parallel_desc_sym, ref_blob_object.parallel_desc_symbol)
            )

    def _GetMut2OperandBlobObjects(
        self, op_attribute, parallel_desc_sym, bn_in_op2blob_object={}
    ):
        mut2_operand_blob_objects = []

        def GetOutBlobParallelDescSymbol(obn):
            parallel_signature = op_attribute.parallel_signature
            bn2symbol_id = parallel_signature.bn_in_op2parallel_desc_symbol_id
            if obn in bn2symbol_id:
                return symbol_storage.GetSymbol4Id(bn2symbol_id[obn])
            else:
                return parallel_desc_sym

        for obn in op_attribute.output_bns:
            obn2modifier = op_attribute.arg_modifier_signature.obn2output_blob_modifier
            if obn2modifier[obn].header_infered_before_compute:
                continue
            obn_sym = self.GetSymbol4String(obn)
            op_arg_parallel_attr = op_arg_util.GetOpArgParallelAttribute(
                GetOutBlobParallelDescSymbol(obn), op_attribute, obn
            )
            op_arg_blob_attr = op_arg_util.GetOpArgBlobAttribute(op_attribute, obn)
            out_blob_object = self._NewBlobObject(
                op_arg_parallel_attr, op_arg_blob_attr
            )
            bn_in_op2blob_object[obn] = out_blob_object
            mut2_operand_blob_objects.append((obn_sym, out_blob_object))
        return mut2_operand_blob_objects

    def _NewBlobObject(self, op_arg_parallel_attr, op_arg_blob_attr):
        object_id = self._NewObjectId(op_arg_parallel_attr.parallel_desc_symbol)
        return object_util.BlobObject(
            object_id=object_id,
            op_arg_parallel_attr=op_arg_parallel_attr,
            op_arg_blob_attr=op_arg_blob_attr,
            release=self.release_blob_object_,
        )

    def _NewSymbolId4String(self, string):
        symbol_id = self._NewSymbolId()
        self._InitStringSymbol(symbol_id, string)
        return symbol_id

    def _NewSymbolId4ParallelConf(self, parallel_conf):
        symbol_id = self.id_generator_.NewSymbolId()
        self._NewParallelConfSymbol(symbol_id, parallel_conf)
        return symbol_id

    def _NewSymbolId4Scope(self, scope_proto):
        symbol_id = self._NewSymbolId()
        scope_proto.symbol_id = symbol_id
        self._NewScopeSymbol(scope_proto)
        return symbol_id

    def _NewSymbolId4JobConf(self, job_conf):
        symbol_id = self._NewSymbolId()
        self._InitJobConfSymbol(symbol_id, job_conf)
        return symbol_id

    def _NewSymbolId4OpConf(self, op_conf):
        symbol_id = self._NewSymbolId()
        self._InitOpConfSymbol(symbol_id, op_conf)
        return symbol_id

    def _NewSymbolId4OpParallelAttribute(self, op_parallel_attribute):
        symbol_id = self._NewSymbolId()
        self._InitOpParallelAttributeSymbol(symbol_id, op_parallel_attribute)
        return symbol_id

    def _NewSharedOpKernelObjectId4ParallelConfSymbolId(self, parallel_desc_sym):
        return self._NewObjectId(parallel_desc_sym)

    def _StatelessCallOpKernel(
        self,
        instr_name,
        parallel_desc_sym,
        job_conf_sym,
        op_conf_sym,
        op_parallel_attribute_sym,
        shared_opkernel_obj,
        const_operand_blob_objects,
        mut1_operand_blob_objects,
        mut2_operand_blob_objects,
    ):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "%s.%s" % (
            parallel_desc_sym.device_tag,
            instr_name,
        )
        instruction.parallel_desc_symbol_id = parallel_desc_sym.symbol_id
        instruction.operand.append(_SymbolOperand(job_conf_sym.symbol_id))
        instruction.operand.append(_SymbolOperand(op_conf_sym.symbol_id))
        instruction.operand.append(_SymbolOperand(op_parallel_attribute_sym.symbol_id))
        instruction.operand.append(_MutOperand(shared_opkernel_obj.object_id))
        instruction.operand.append(_OperandSeparator())
        for ibn_sym, _ in const_operand_blob_objects:
            instruction.operand.append(_SymbolOperand(ibn_sym.symbol_id))
        for _, blob_object in const_operand_blob_objects:
            instruction.operand.append(_ConstOperand(blob_object.object_id))
        instruction.operand.append(_OperandSeparator())
        for obn_sym, _ in mut1_operand_blob_objects:
            instruction.operand.append(_SymbolOperand(obn_sym.symbol_id))
        for _, blob_object in mut1_operand_blob_objects:
            instruction.operand.append(_MutOperand(blob_object.object_id))
        instruction.operand.append(_OperandSeparator())
        for obn_sym, _ in mut2_operand_blob_objects:
            instruction.operand.append(_SymbolOperand(obn_sym.symbol_id))
        for _, blob_object in mut2_operand_blob_objects:
            instruction.operand.append(_Mut2Operand(blob_object.object_id))
        self.instruction_list_.instruction.append(instruction)

    def _NewSymbolId(self):
        symbol_id = self.id_generator_.NewSymbolId()
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "NewSymbol"
        instruction.operand.append(_Int64Operand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        return symbol_id

    def _NewObjectId(self, parallel_desc_sym):
        object_id = self.id_generator_.NewObjectId()
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "NewObject"
        instruction.parallel_desc_symbol_id = parallel_desc_sym.symbol_id
        instruction.operand.append(_Int64Operand(object_id))
        self.instruction_list_.instruction.append(instruction)
        return object_id

    def _LazyReference(self, blob_object, var_op_name):
        instruction = instr_util.InstructionProto()
        device_tag = blob_object.parallel_desc_symbol.device_tag
        instruction.instr_type_name = "{}.LazyReference".format(device_tag)
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_MutOperand(blob_object.object_id))
        var_op_name_sym = self.GetSymbol4String(var_op_name + "/out")
        instruction.operand.append(_SymbolOperand(var_op_name_sym.symbol_id))
        self.instruction_list_.instruction.append(instruction)

    def _BroadcastObjectReference(self, sole_mirrored_object, parallel_desc_sym):
        object_id = self.id_generator_.NewObjectId()
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "BroadcastObjectReference"
        instruction.parallel_desc_symbol_id = parallel_desc_sym.symbol_id
        instruction.operand.append(_Int64Operand(object_id))
        instruction.operand.append(_Int64Operand(sole_mirrored_object.object_id))
        self.instruction_list_.instruction.append(instruction)
        return object_id

    def _InitStringSymbol(self, symbol_id, string):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "InitStringSymbol"
        instruction.operand.append(_InitSymbolOperand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = symbol_id
        eager_symbol.string_symbol = string
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _NewParallelConfSymbol(self, symbol_id, parallel_conf):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "NewParallelDescSymbol"
        instruction.operand.append(_Int64Operand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = symbol_id
        eager_symbol.parallel_conf_symbol.CopyFrom(parallel_conf)
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _NewScopeSymbol(self, scope_proto):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "InitScopeSymbol"
        instruction.operand.append(_InitSymbolOperand(scope_proto.symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = scope_proto.symbol_id
        eager_symbol.scope_symbol.CopyFrom(scope_proto)
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _InitJobConfSymbol(self, symbol_id, job_conf):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "InitJobDescSymbol"
        instruction.operand.append(_InitSymbolOperand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = symbol_id
        eager_symbol.job_conf_symbol.CopyFrom(job_conf)
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _InitOpConfSymbol(self, symbol_id, op_conf):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "InitOperatorConfSymbol"
        instruction.operand.append(_InitSymbolOperand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = symbol_id
        eager_symbol.op_conf_symbol.CopyFrom(op_conf)
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _InitOpParallelAttributeSymbol(self, symbol_id, op_parallel_attribute):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "InitOpParallelAttributeSymbol"
        instruction.operand.append(_InitSymbolOperand(symbol_id))
        self.instruction_list_.instruction.append(instruction)
        eager_symbol = eager_symbol_util.EagerSymbol()
        eager_symbol.symbol_id = symbol_id
        eager_symbol.op_parallel_attribute_symbol.CopyFrom(op_parallel_attribute)
        self.eager_symbol_list_.eager_symbol.append(eager_symbol)

    def _FetchBlob(self, instruction_name, blob_object, fetcher):
        unique_callback_id = python_callback.GetIdForRegisteredCallback(fetcher)
        instruction = instr_util.InstructionProto()
        device_tag = blob_object.parallel_desc_symbol.device_tag
        instruction.instr_type_name = "%s.%s" % (device_tag, instruction_name)
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_ConstOperand(blob_object.object_id))
        instruction.operand.append(_Int64Operand(unique_callback_id))
        self.instruction_list_.instruction.append(instruction)

    def FeedBlob(self, blob_object, feeder):
        unique_callback_id = python_callback.GetIdForRegisteredCallback(feeder)
        instruction = instr_util.InstructionProto()
        device_tag = blob_object.parallel_desc_symbol.device_tag
        instruction.instr_type_name = "%s.%s" % (device_tag, "FeedBlob")
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_Mut2Operand(blob_object.object_id))
        instruction.operand.append(_Int64Operand(unique_callback_id))
        self.instruction_list_.instruction.append(instruction)

    def _TryClearObject(self, blob_object):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "TryClearObject"
        instruction.parallel_desc_symbol_id = blob_object.parallel_desc_symbol.symbol_id
        instruction.operand.append(_MutOperand(blob_object.object_id))
        self.instruction_list_.instruction.append(instruction)

    def _DeleteObject(self, blob_object):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "DeleteObject"
        instruction.operand.append(_DelObjectOperand(blob_object.object_id))
        self.instruction_list_.instruction.append(instruction)

    def _ReplaceMirrored(self, parallel_desc_sym, lhs_objects, rhs_objects):
        instruction = instr_util.InstructionProto()
        instruction.instr_type_name = "ReplaceMirrored"
        instruction.parallel_desc_symbol_id = parallel_desc_sym.symbol_id
        for lhs_object in lhs_objects:
            instruction.operand.append(_Int64Operand(lhs_object.object_id))
        instruction.operand.append(_OperandSeparator())
        for rhs_object in rhs_objects:
            instruction.operand.append(_Int64Operand(rhs_object.object_id))
        self.instruction_list_.instruction.append(instruction)


def _SymbolOperand(val):
    operand = instr_util.InstructionOperandProto()
    _SetSoleMirroredOperand(operand.symbol_operand, val)
    return operand


def _InitSymbolOperand(val):
    operand = instr_util.InstructionOperandProto()
    _SetSoleMirroredOperand(operand.init_symbol_operand, val)
    return operand


def _ConstOperand(val):
    operand = instr_util.InstructionOperandProto()
    _SetMirroredOperand(operand.const_operand, val)
    return operand


def _MutOperand(val):
    operand = instr_util.InstructionOperandProto()
    _SetMirroredOperand(operand.mut_operand, val)
    return operand


def _Mut2Operand(val):
    operand = instr_util.InstructionOperandProto()
    _SetMirroredOperand(operand.mut2_operand, val)
    return operand


def _DelObjectOperand(val):
    operand = instr_util.InstructionOperandProto()
    _SetAllMirroredOperand(operand.mut_operand, val)
    return operand


def _Int64Operand(val):
    operand = instr_util.InstructionOperandProto()
    operand.int64_operand = val
    return operand


def _OperandSeparator():
    operand = instr_util.InstructionOperandProto()
    operand.separator.SetInParent()
    return operand


def _SetMirroredOperand(operand, val):
    operand.logical_object_id = val
    operand.current_global_device_id.SetInParent()


def _SetSoleMirroredOperand(operand, val):
    operand.logical_object_id = val
    operand.sole_mirrored_object.SetInParent()


def _SetAllMirroredOperand(operand, val):
    operand.logical_object_id = val
    operand.all_mirrored_object.SetInParent()


def _FindOrCreateDelegateBlobObject(builder, x_blob_object, op_arg_parallel_attr):
    if x_blob_object.op_arg_parallel_attr == op_arg_parallel_attr:
        return x_blob_object
    blob_cache = blob_cache_util.FindOrCreateBlobCache(x_blob_object)

    def Fetch(x_blob_object, op_arg_parallel_attr):
        return boxing_util.BoxingTo(builder, x_blob_object, op_arg_parallel_attr)

    return blob_cache.GetCachedDelegateBlobObject(op_arg_parallel_attr, Fetch)


def _GetOpConfBlobNameAttr(pb_message, field):
    if hasattr(pb_message, field):
        return getattr(pb_message, field)
    m = re.search("_(\d+)$", field)
    assert m is not None
    blob_name = field[0 : -len(m.group(0))]
    index = int(m.group(0)[1:])
    assert hasattr(pb_message, blob_name), (pb_message, blob_name)
    repeated_field = getattr(pb_message, blob_name)
    assert index >= 0
    assert index < len(repeated_field)
    return repeated_field[index]


def _ReleaseLogicalBlobObject(blob_object):
    LogicalRun(lambda builder: builder.DeleteBlob(blob_object))


def _ReleasePhysicalBlobObject(blob_object):
    PhysicalRun(lambda builder: builder.DeleteBlob(blob_object))
